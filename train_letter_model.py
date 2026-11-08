"""Train the handwritten letter classifier used by handwriting_ocr.py.

Data: EMNIST "letters" split (124 800 handwritten letters, 26 classes, upper and lower case merged).
Download it once:
    curl -L -o .cache/emnist_gzip.zip https://biometrics.nist.gov/cs_links/EMNIST/gzip.zip
    unzip -j .cache/emnist_gzip.zip 'gzip/emnist-letters-*' -d .cache/emnist

Optional own samples (e.g. glyphs from real scans) can be added with --extra-dir; the directory must
contain one subdirectory per letter (A, B, ...) with 28x28 grayscale PNGs, white letter on black.

Usage:
    python train_letter_model.py [--epochs 8] [--extra-dir samples] [--output models/letter_cnn.pt]
"""
import argparse
import gzip
import os
import time

import cv2
import numpy as np
import torch
import torch.nn.functional as F

from handwriting_ocr import DEFAULT_MODEL_PATH, EMNIST_CLASSES, build_model


def load_idx(path):
    with gzip.open(path, "rb") as f:
        data = f.read()
    dims = data[3]
    shape = [int.from_bytes(data[4 + 4 * i:8 + 4 * i], "big") for i in range(dims)]
    return np.frombuffer(data, np.uint8, offset=4 + 4 * dims).reshape(shape)


def load_emnist(directory, split):
    images = load_idx(os.path.join(directory, f"emnist-letters-{split}-images-idx3-ubyte.gz"))
    labels = load_idx(os.path.join(directory, f"emnist-letters-{split}-labels-idx1-ubyte.gz"))
    # EMNIST images are stored transposed; labels are 1..26
    images = images.transpose(0, 2, 1).astype(np.float32) / 255.0
    return images, labels.astype(np.int64) - 1


def load_extra(directory):
    images, labels = [], []
    for letter in sorted(os.listdir(directory)):
        if letter.upper() not in EMNIST_CLASSES:
            continue
        for name in os.listdir(os.path.join(directory, letter)):
            img = cv2.imread(os.path.join(directory, letter, name), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                images.append(cv2.resize(img, (28, 28)).astype(np.float32) / 255.0)
                labels.append(EMNIST_CLASSES.index(letter.upper()))
    return np.array(images, np.float32).reshape(-1, 28, 28), np.array(labels, np.int64)


def augment(batch):
    """Random affine distortion and stroke width changes, so the model copes with real pens."""
    n = batch.shape[0]
    angle = (torch.rand(n) - 0.5) * 2 * np.radians(15)
    scale = 0.8 + torch.rand(n) * 0.3
    shear = (torch.rand(n) - 0.5) * 0.4
    tx, ty = (torch.rand(n) - 0.5) * 0.2, (torch.rand(n) - 0.5) * 0.2
    cos, sin = torch.cos(angle) / scale, torch.sin(angle) / scale
    theta = torch.stack([torch.stack([cos, -sin + shear, tx], 1), torch.stack([sin, cos, ty], 1)], 1)
    grid = F.affine_grid(theta, batch.shape, align_corners=False)
    batch = F.grid_sample(batch, grid, align_corners=False)

    # Thicker or thinner strokes for a random part of the batch
    mode = torch.rand(n)
    thick = F.max_pool2d(batch, 3, 1, 1)
    thin = -F.max_pool2d(-batch, 3, 1, 1)
    batch = torch.where((mode < 0.3)[:, None, None, None], thick, batch)
    batch = torch.where((mode > 0.85)[:, None, None, None], thin, batch)
    return batch.clamp(0, 1)


def evaluate(model, images, labels, batch_size=2048):
    model.eval()
    correct = 0
    with torch.no_grad():
        for i in range(0, len(images), batch_size):
            x = torch.from_numpy(images[i:i + batch_size, None])
            correct += (model(x).argmax(1).numpy() == labels[i:i + batch_size]).sum()
    return correct / len(images)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--emnist-dir", default=os.path.join(".cache", "emnist"))
    parser.add_argument("--extra-dir", help="own labelled samples, one subdirectory per letter")
    parser.add_argument("--extra-weight", type=int, default=50, help="how many times to repeat own samples")
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--output", default=DEFAULT_MODEL_PATH)
    args = parser.parse_args()

    torch.manual_seed(0)
    train_x, train_y = load_emnist(args.emnist_dir, "train")
    test_x, test_y = load_emnist(args.emnist_dir, "test")
    if args.extra_dir:
        extra_x, extra_y = load_extra(args.extra_dir)
        print(f"Own samples: {len(extra_x)}")
        if len(extra_x):
            train_x = np.concatenate([train_x] + [extra_x] * args.extra_weight)
            train_y = np.concatenate([train_y] + [extra_y] * args.extra_weight)

    model = build_model()
    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-4)
    steps_per_epoch = int(np.ceil(len(train_x) / 256))
    scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, max_lr=3e-3, total_steps=args.epochs * steps_per_epoch)

    for epoch in range(args.epochs):
        model.train()
        start = time.time()
        order = np.random.permutation(len(train_x))
        for i in range(0, len(order), 256):
            idx = order[i:i + 256]
            x = augment(torch.from_numpy(train_x[idx, None]))
            loss = F.cross_entropy(model(x), torch.from_numpy(train_y[idx]), label_smoothing=0.05)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()
        acc = evaluate(model, test_x, test_y)
        print(f"epoch {epoch + 1}/{args.epochs}: loss {loss.item():.3f}, EMNIST test accuracy {acc:.2%}, "
              f"{time.time() - start:.0f}s")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    torch.save(model.state_dict(), args.output)
    print(f"Model saved to {args.output}")


if __name__ == "__main__":
    main()
