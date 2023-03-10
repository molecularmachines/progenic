import torch
from argparse import ArgumentParser
import pytorch_lightning as pl
from preprocess import trim_dataset, standardize_dataset, StandardizeTransform
from en_denoiser import EnDenoiser
import sidechainnet as scn
from collate import prepare_dataloaders
from moleculib.protein.dataset import ProteinDataset
from moleculib.protein.loader import ProteinDataLoader
from moleculib.protein.batch import PadBatch
from torch.utils.data import DataLoader


def cli_main():
    # TODO: does this affect random noise?
    pl.seed_everything(42)

    # ------------
    # args
    # ------------
    parser = ArgumentParser()
    parser.add_argument('--batch_size', default=4, type=int)
    parser.add_argument('--device', default='1', type=str)
    parser = pl.Trainer.add_argparse_args(parser)
    parser = EnDenoiser.add_model_specific_args(parser)
    args = parser.parse_args()
    device = "gpu" if torch.cuda.is_available() else "cpu"

    # ------------
    # data
    # ------------
    TRAIN_DIR = "data/cath_sanity/train"
    VAL_DIR = "data/cath_sanity/val"
    TEST_DIR = "data/cath_sanity/test"

    transform = StandardizeTransform()

    train_dataset = ProteinDataset(TRAIN_DIR, transform=[transform], preload=True)
    train_loader = DataLoader(train_dataset,
                              collate_fn=PadBatch.collate,
                              batch_size=args.batch_size)

    val_dataset = ProteinDataset(VAL_DIR, transform=[transform], preload=True)
    val_loader = DataLoader(val_dataset,
                            collate_fn=PadBatch.collate,
                            batch_size=args.batch_size)

    test_dataset = ProteinDataset(TEST_DIR, transform=[transform], preload=True)
    test_loader = DataLoader(test_dataset,
                             collate_fn=PadBatch.collate,
                             batch_size=args.batch_size)

    # ------------
    # model
    # ------------
    model = EnDenoiser()

    # ------------
    # training
    # ------------
    trainer = pl.Trainer.from_argparse_args(args, accelerator=device, devices=args.device)
    trainer.fit(model, train_loader, val_loader)

    # ------------
    # testing
    # ------------
    trainer.test(dataloaders=test_loader)


if __name__ == '__main__':
    cli_main()
