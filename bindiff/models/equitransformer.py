import torch
from torch import nn, einsum
from torch.utils.checkpoint import checkpoint_sequential

from einops import rearrange, repeat
from models.modules import (
    DynamicPositionBias,
    SinusoidalPositionEmbeddings,
    LayerNorm,
    CoorsNorm,
    Residual,
    GEGLU
)
from models.helpers import (
    exists,
    max_neg_value,
    default,
    l2norm,
    batched_index_select
)


class FeedForward(nn.Module):
    def __init__(
        self,
        *,
        dim,
        mult=4,
        dropout=0.
    ):
        super().__init__()
        inner_dim = int(dim * mult * 2 / 3)

        self.net = nn.Sequential(
            LayerNorm(dim),
            nn.Linear(dim, inner_dim * 2, bias=False),
            GEGLU(),
            LayerNorm(inner_dim),
            nn.Dropout(dropout),
            nn.Linear(inner_dim, dim, bias=False)
        )

    def forward(self, feats, coors):
        return self.net(feats), 0


class EquivariantAttention(nn.Module):
    def __init__(
        self,
        *,
        dim,
        time_emb_dim=128,
        dim_head=64,
        heads=4,
        edge_dim=0,
        coors_hidden_dim=16,
        neighbors=0,
        only_sparse_neighbors=False,
        valid_neighbor_radius=float('inf'),
        init_eps=1e-3,
        rel_pos_emb=None,
        edge_mlp_mult=2,
        norm_rel_coors=True,
        norm_coors_scale_init=1.,
        use_cross_product=False,
        talking_heads=False,
        scale=8,
        dropout=0.
    ):
        super().__init__()
        self.scale = scale
        self.dim = dim

        # time embedding to out dim
        self.time_mlp = (
            nn.Sequential(nn.SiLU(), nn.Linear(time_emb_dim, dim * 2))
            if exists(time_emb_dim)
            else None
        )

        self.norm = LayerNorm(dim)

        self.neighbors = neighbors
        self.only_sparse_neighbors = only_sparse_neighbors
        self.valid_neighbor_radius = valid_neighbor_radius

        attn_inner_dim = heads * dim_head
        self.heads = heads
        self.to_qkv = nn.Linear(dim, attn_inner_dim * 3, bias=False)
        self.to_out = nn.Linear(attn_inner_dim, dim)

        self.talking_heads = nn.Conv2d(heads, heads, 1, bias=False) if talking_heads else None

        self.edge_mlp = None
        has_edges = edge_dim > 0

        if has_edges:
            edge_input_dim = heads + edge_dim
            edge_hidden = edge_input_dim * edge_mlp_mult

            self.edge_mlp = nn.Sequential(
                nn.Linear(edge_input_dim, edge_hidden, bias=False),
                nn.GELU(),
                nn.Linear(edge_hidden, heads, bias=False)
            )

            self.coors_mlp = nn.Sequential(
                nn.GELU(),
                nn.Linear(heads, heads, bias=False)
            )
        else:
            self.coors_mlp = nn.Sequential(
                nn.Linear(heads, coors_hidden_dim, bias=False),
                nn.GELU(),
                nn.Linear(coors_hidden_dim, heads, bias=False)
            )

        self.coors_gate = nn.Sequential(
            nn.Linear(heads, heads),
            nn.Tanh()
        )

        self.use_cross_product = use_cross_product
        if use_cross_product:
            self.cross_coors_mlp = nn.Sequential(
                nn.Linear(heads, coors_hidden_dim, bias=False),
                nn.GELU(),
                nn.Linear(coors_hidden_dim, heads * 2, bias=False)
            )

        self.norm_rel_coors = CoorsNorm(scale_init=norm_coors_scale_init) if norm_rel_coors else nn.Identity()

        num_coors_combine_heads = (2 if use_cross_product else 1) * heads
        self.coors_combine = nn.Parameter(torch.randn(num_coors_combine_heads))

        # positional embedding
        # for both along the sequence (if specified by rel_pos_emb)
        # and the relative distance between each residue / atom

        self.rel_pos_emb = rel_pos_emb

        self.dynamic_pos_bias_mlp = DynamicPositionBias(
            dim=dim // 2,
            heads=heads,
            dim_head=dim_head,
            depth=3,
            input_dim=(2 if rel_pos_emb else 1)
        )

        # dropouts
        self.node_dropout = nn.Dropout(dropout)
        self.coor_dropout = nn.Dropout(dropout)

        # init
        self.init_eps = init_eps
        self.apply(self.init_)

    def init_(self, module):
        if type(module) in {nn.Linear}:
            nn.init.normal_(module.weight, std=self.init_eps)

    def forward(
        self,
        feats,
        coors,
        time_emb=None,
        edges=None,
        mask=None,
        adj_mat=None
    ):
        b, n, _ = coors.size()
        h = self.heads
        num_nn = self.neighbors
        only_sparse_neighbors = self.only_sparse_neighbors
        valid_neighbor_radius = self.valid_neighbor_radius
        device = coors.device

        # layer norm
        feats = self.norm(feats)

        assert not (only_sparse_neighbors and not exists(adj_mat)), 'adjacency matrix must be passed in if only_sparse_neighbors is turned on'

        # calculate coords relative distances
        rel_coors = rearrange(coors, 'b i d -> b i () d') - rearrange(coors, 'b j d -> b () j d')
        rel_dist = rel_coors.norm(p=2, dim=-1)

        # calculate neighborhood indices
        nbhd_indices = None
        nbhd_masks = None
        nbhd_ranking = rel_dist.clone()

        # apply adjacency matrix
        if exists(adj_mat):
            if len(adj_mat.shape) == 2:
                adj_mat = repeat(adj_mat, 'i j -> b i j', b=b)

            self_mask = torch.eye(n, device=device).bool()
            self_mask = rearrange(self_mask, 'i j -> () i j')
            adj_mat.masked_fill_(self_mask, False)

            max_adj_neighbors = adj_mat.long().sum(dim=-1).max().item() + 1

            num_nn = max_adj_neighbors if only_sparse_neighbors else (num_nn + max_adj_neighbors)
            valid_neighbor_radius = 0 if only_sparse_neighbors else valid_neighbor_radius

            nbhd_ranking = nbhd_ranking.masked_fill(self_mask, -1.)
            nbhd_ranking = nbhd_ranking.masked_fill(adj_mat, 0.)

        if 0 < num_nn < n:
            # make sure padding does not end up becoming neighbors
            if exists(mask):
                ranking_mask = mask[:, :, None] * mask[:, None, :]
                nbhd_ranking = nbhd_ranking.masked_fill(~ranking_mask, 1e5)

            nbhd_values, nbhd_indices = nbhd_ranking.topk(num_nn, dim = -1, largest = False)
            nbhd_masks = nbhd_values <= valid_neighbor_radius

        # derive queries keys and values
        q, k, v = self.to_qkv(feats).chunk(3, dim=-1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), (q, k, v))

        # calculate nearest neighbors
        i = j = n

        if exists(nbhd_indices):
            i, j = nbhd_indices.shape[-2:]
            nbhd_indices_with_heads = repeat(nbhd_indices, 'b n d -> b h n d', h=h)
            k = batched_index_select(k, nbhd_indices_with_heads, dim=2)
            v = batched_index_select(v, nbhd_indices_with_heads, dim=2)
            rel_dist = batched_index_select(rel_dist, nbhd_indices, dim=2)
            rel_coors = batched_index_select(rel_coors, nbhd_indices, dim=2)
        else:
            k = repeat(k, 'b h j d -> b h n j d', n = n)
            v = repeat(v, 'b h j d -> b h n j d', n = n)

        # prepare mask
        if exists(mask):
            q_mask = rearrange(mask, 'b i -> b 1 i 1')
            k_mask = repeat(mask, 'b j -> b i j', i = n)

            if exists(nbhd_indices):
                k_mask = batched_index_select(k_mask, nbhd_indices, dim = 2)

            k_mask = rearrange(k_mask, 'b i j -> b 1 i j')

            mask = q_mask * k_mask

            if exists(nbhd_masks):
                mask &= rearrange(nbhd_masks, 'b i j -> b 1 i j')

        # cosine sim attention
        q, k = map(l2norm, (q, k))

        # generate and apply rotary embeddings
        rel_dist = -100 * rel_dist
        rel_dist = rearrange(rel_dist, 'b i j -> b 1 i j 1')

        if self.rel_pos_emb:
            seq = torch.arange(n, device=device, dtype=q.dtype)
            seq_target_pos = nbhd_indices if exists(nbhd_indices) else rearrange(seq, 'j -> 1 j')
            seq_rel_dist = rearrange(seq, 'i -> i 1') - seq_target_pos
            seq_rel_dist = repeat(seq_rel_dist, 'i j -> b 1 i j 1', b=b)
            rel_dist = torch.cat((rel_dist, seq_rel_dist), dim=-1)

        qk_pos, value_pos = self.dynamic_pos_bias_mlp(rel_dist)

        # calculate inner product for queries and keys
        qk = einsum('b h i d, b h i j d -> b h i j', q, k) * (self.scale if not exists(edges) else 1)
        qk = qk + qk_pos
        v = v + value_pos

        # add edge information and pass through edges MLP if needed
        if exists(edges):
            if exists(nbhd_indices):
                edges = batched_index_select(edges, nbhd_indices, dim = 2)

            qk = rearrange(qk, 'b h i j -> b i j h')
            qk = torch.cat((qk, edges), dim = -1)
            qk = self.edge_mlp(qk)
            qk = rearrange(qk, 'b i j h -> b h i j')

        # coordinate MLP and calculate coordinate updates
        coors_mlp_input = rearrange(qk, 'b h i j -> b i j h')
        coor_weights = self.coors_mlp(coors_mlp_input)

        if exists(mask):
            mask_value = max_neg_value(coor_weights)
            coor_mask = repeat(mask, 'b 1 i j -> b i j 1')
            coor_weights.masked_fill_(~coor_mask, mask_value)

        coor_attn = coor_weights.softmax(dim=-2)
        coor_attn = self.coor_dropout(coor_attn)

        rel_coors_sign = self.coors_gate(coors_mlp_input)
        rel_coors_sign = rearrange(rel_coors_sign, 'b i j h -> b i j 1 h')

        if self.use_cross_product:
            rel_coors_i = repeat(rel_coors, 'b n i c -> b n (i j) c', j=j)
            rel_coors_j = repeat(rel_coors, 'b n j c -> b n (i j) c', i=j)

            cross_coors = torch.cross(rel_coors_i, rel_coors_j, dim=-1)

            cross_coors = self.norm_rel_coors(cross_coors)
            cross_coors = repeat(cross_coors, 'b i j c -> b i j c h', h=h)

        rel_coors = self.norm_rel_coors(rel_coors)
        rel_coors = repeat(rel_coors, 'b i j c -> b i j c h', h=h)

        rel_coors = rel_coors * rel_coors_sign

        # cross product
        if self.use_cross_product:
            cross_weights = self.cross_coors_mlp(coors_mlp_input)

            cross_weights = rearrange(cross_weights, 'b i j (h n) -> b i j h n', n=2)
            cross_weights_i, cross_weights_j = cross_weights.unbind(dim=-1)

            cross_weights = rearrange(cross_weights_i, 'b n i h -> b n i 1 h') + rearrange(cross_weights_j, 'b n j h -> b n 1 j h')

            if exists(mask):
                cross_mask = (coor_mask[:, :, :, None, :] & coor_mask[:, :, None, :, :])
                cross_weights = cross_weights.masked_fill(~cross_mask, mask_value)

            cross_weights = rearrange(cross_weights, 'b n i j h -> b n (i j) h')
            cross_attn = cross_weights.softmax(dim=-2)

        # aggregate and combine heads for coordinate updates

        rel_out = einsum('b i j h, b i j c h -> b i c h', coor_attn, rel_coors)

        if self.use_cross_product:
            cross_out = einsum('b i j h, b i j c h -> b i c h', cross_attn, cross_coors)
            rel_out = torch.cat((rel_out, cross_out), dim=-1)

        coors_out = einsum('b n c h, h -> b n c', rel_out, self.coors_combine)

        # derive attention

        sim = qk.clone()

        if exists(mask):
            mask_value = max_neg_value(sim)
            sim.masked_fill_(~mask, mask_value)

        attn = sim.softmax(dim=-1)
        attn = self.node_dropout(attn)

        if exists(self.talking_heads):
            attn = self.talking_heads(attn)

        # weighted sum of values and combine heads

        out = einsum('b h i j, b h i j d -> b h i d', attn, v)

        out = rearrange(out, 'b h n d -> b n (h d)')
        out = self.to_out(out)

        # time embedding
        if exists(time_emb):
            time_emb = self.time_mlp(time_emb)
            time_emb = rearrange(time_emb, "b c -> b 1 c")
            scale_shift = time_emb.chunk(2, dim=-1)
            scale, shift = scale_shift
            out = out * (scale + 1) + shift

        return out, coors_out


class Block(nn.Module):
    def __init__(self, attn, ff):
        super().__init__()
        self.attn = attn
        self.ff = ff

    def forward(self, inp, time_emb=None, coor_changes=None):
        feats, coors, mask, edges, adj_mat = inp
        feats, coors = self.attn(feats, coors, time_emb=time_emb, edges=edges, mask=mask, adj_mat=adj_mat)

        feats, coors = self.ff(feats, coors)
        return (feats, coors, mask, edges, adj_mat)


class EnTransformer(nn.Module):
    def __init__(
        self,
        *,
        dim,
        depth,
        num_tokens=None,
        rel_pos_emb=False,
        dim_head=64,
        heads=8,
        num_edge_tokens=None,
        edge_dim=0,
        coors_hidden_dim=16,
        neighbors=0,
        only_sparse_neighbors=False,
        num_adj_degrees=None,
        adj_dim=0,
        valid_neighbor_radius=float('inf'),
        init_eps=1e-3,
        norm_rel_coors=True,
        norm_coors_scale_init=1.,
        use_cross_product=False,
        talking_heads=False,
        checkpoint=False,
        attn_dropout=0.,
        ff_dropout=0.
    ):
        super().__init__()
        assert dim_head >= 32, 'your dimension per head should be greater than 32 for rotary embeddings to work well'
        assert not (exists(num_adj_degrees) and num_adj_degrees < 1), 'make sure adjacent degrees is greater than 1'

        if only_sparse_neighbors:
            num_adj_degrees = default(num_adj_degrees, 1)

        self.dim = dim
        self.token_emb = nn.Embedding(num_tokens, dim) if exists(num_tokens) else None
        self.edge_emb = nn.Embedding(num_edge_tokens, edge_dim) if exists(num_edge_tokens) else None

        # time embeddings
        time_dim = dim * 4

        self.time_mlp = nn.Sequential(
            SinusoidalPositionEmbeddings(dim),
            nn.Linear(dim, time_dim),
            nn.GELU(),
            nn.Linear(time_dim, time_dim),
        )

        self.num_adj_degrees = num_adj_degrees
        self.adj_emb = nn.Embedding(num_adj_degrees + 1, adj_dim) if exists(num_adj_degrees) and adj_dim > 0 else None
        adj_dim = adj_dim if exists(num_adj_degrees) else 0

        self.checkpoint = checkpoint
        self.layers = nn.ModuleList([])

        for ind in range(depth):
            self.layers.append(Block(
                Residual(EquivariantAttention(
                    dim=dim,
                    time_emb_dim=time_dim,
                    dim_head=dim_head,
                    heads=heads,
                    coors_hidden_dim=coors_hidden_dim,
                    edge_dim=(edge_dim + adj_dim),
                    neighbors=neighbors,
                    only_sparse_neighbors=only_sparse_neighbors,
                    valid_neighbor_radius=valid_neighbor_radius,
                    init_eps=init_eps,
                    rel_pos_emb=rel_pos_emb,
                    norm_rel_coors=norm_rel_coors,
                    norm_coors_scale_init=norm_coors_scale_init,
                    use_cross_product=use_cross_product,
                    talking_heads=talking_heads,
                    dropout=attn_dropout
                )),
                Residual(FeedForward(
                    dim=dim,
                    dropout=ff_dropout
                ))
            ))

    def forward(
        self,
        coors,
        timesteps,
        context=None,
        edges=None,
        mask=None,
        adj_mat=None,
        return_coor_changes=False,
        **kwargs
    ):
        b, seqlen, _ = coors.shape
        t = self.time_mlp(timesteps)

        # initialize features to ones
        feats = torch.ones((b, seqlen, self.dim)).to(coors)

        # context token embeddings
        if exists(self.token_emb) and exists(context):
            context_seq_emb = self.token_emb(context)
            feats = context_seq_emb.to(coors)

        if exists(self.edge_emb):
            assert exists(edges), 'edges must be passed in as (batch x seq x seq) indicating edge type'
            edges = self.edge_emb(edges)

        assert not (exists(adj_mat) and (not exists(self.num_adj_degrees) or self.num_adj_degrees == 0)), 'num_adj_degrees must be greater than 0 if you are passing in an adjacency matrix'

        if exists(self.num_adj_degrees):
            assert exists(adj_mat), 'adjacency matrix must be passed in (keyword argument adj_mat)'

            if len(adj_mat.shape) == 2:
                adj_mat = repeat(adj_mat.clone(), 'i j -> b i j', b=b)

            adj_indices = adj_mat.clone().long()

            for ind in range(self.num_adj_degrees - 1):
                degree = ind + 2

                next_degree_adj_mat = (adj_mat.float() @ adj_mat.float()) > 0
                next_degree_mask = (next_degree_adj_mat.float() - adj_mat.float()).bool()
                adj_indices.masked_fill_(next_degree_mask, degree)
                adj_mat = next_degree_adj_mat.clone()

            if exists(self.adj_emb):
                adj_emb = self.adj_emb(adj_indices)
                edges = torch.cat((edges, adj_emb), dim = -1) if exists(edges) else adj_emb

        assert not (return_coor_changes and self.training), 'you must be eval mode in order to return coordinates'

        # go through layers
        coor_changes = [coors]
        inp = (feats, coors, mask, edges, adj_mat)

        # if in training mode and checkpointing is designated, use checkpointing across blocks to save memory
        if self.training and self.checkpoint:
            inp = checkpoint_sequential(self.layers, len(self.layers), inp)
        else:
            # iterate through blocks
            for layer in self.layers:
                inp = layer(inp, time_emb=t)
                coor_changes.append(inp[1])  # append coordinates for visualization

        # return
        feats, coors, *_ = inp

        if return_coor_changes:
            return feats, coors, coor_changes

        return feats, coors
