from tqdm import tqdm
from utils import _normalize, _one_hot, get_dataset, accuracy
from utils_jax import cross_entropy_loss, mse_loss
import torchvision.datasets as datasets
import neural_tangents as nt
from attacks.projected_gradient_descent import projected_gradient_descent
from models.cnn_infinite import ConvGroup
from models.dnn_infinite import DenseGroup
from neural_tangents import stax
from torch.utils.data import DataLoader
import PIL
import os
import argparse
import jax.numpy as np
from jax.api import grad, jit, vmap
from jax import random
from jax.config import config

config.update("jax_enable_x64", True)


parser = argparse.ArgumentParser(description="Generate NTGA attack!")
parser.add_argument(
    "--model_type",
    default="cnn",
    type=str,
    help="cnn",
)
parser.add_argument("--dataset", default="c10", type=str, help="c10")
parser.add_argument(
    "--t", default=64, type=int, help="time step used to compute poisoned data"
)
parser.add_argument(
    "--eps", default=8 / 255, type=float, help="epsilon. Strength of NTGA"
)
parser.add_argument(
    "--nb_iter",
    default=10,
    type=int,
    help="number of iteration used to generate poisoned data",
)
parser.add_argument("--block_size", default=512,
                    type=int, help="block size of B-NTGA")
parser.add_argument("--batch_size", default=30, type=int, help="batch size")
parser.add_argument(
    "--save_path",
    default="..dataset/ntga_poisons",
    type=str,
    help="path to save poisoned data",
)
parser.add_argument(
    "--cuda_visible_devices",
    default="0",
    type=str,
    help="specify which GPU to run \
                    an application on",
)

args = parser.parse_args()
os.environ["CUDA_VISIBLE_DEVICES"] = args.cuda_visible_devices


# Epsilon, attack iteration, and step size
if args.dataset == "c10":
    num_classes = 10
    train_size = 50000
    args.eps = 8 / 255
else:
    raise ValueError(
        "To load custom dataset, please modify the code directly.")
eps_iter = (args.eps / args.nb_iter) * 1.1

seed = 0


def surrogate_fn(model_type, W_std, b_std, num_classes):
    if model_type == "cnn":
        init_fn, apply_fn, kernel_fn = stax.serial(
            ConvGroup(2, 64, (2, 2), W_std, b_std),
            stax.Flatten(),
            stax.Dense(384, W_std, b_std),
            stax.Dense(192, W_std, b_std),
            stax.Dense(num_classes, W_std, b_std),
        )
    else:
        raise ValueError
    return init_fn, apply_fn, kernel_fn


def model_fn(
    kernel_fn,
    x_train=None,
    x_test=None,
    fx_train_0=0.0,
    fx_test_0=0.0,
    t=None,
    y_train=None,
    diag_reg=1e-4,
):
    # Kernel
    ntk_train_train = kernel_fn(x_train, x_train, "ntk")
    ntk_test_train = kernel_fn(x_test, x_train, "ntk")

    # Prediction
    predict_fn = nt.predict.gradient_descent_mse(
        ntk_train_train, y_train, diag_reg=diag_reg
    )
    return predict_fn(t, fx_train_0, fx_test_0, ntk_test_train)


def adv_loss(
    x_train,
    x_test,
    y_train,
    y_test,
    kernel_fn,
    loss="mse",
    t=None,
    targeted=False,
    diag_reg=1e-4,
):
    # Kernel
    ntk_train_train = kernel_fn(x_train, x_train, "ntk")
    ntk_test_train = kernel_fn(x_test, x_train, "ntk")

    # Prediction
    predict_fn = nt.predict.gradient_descent_mse(
        ntk_train_train, y_train, diag_reg=diag_reg
    )
    fx = predict_fn(t, 0.0, 0.0, ntk_test_train)[1]

    # Loss
    if loss == "cross-entropy":
        loss = cross_entropy_loss(fx, y_test)
    elif loss == "mse":
        loss = mse_loss(fx, y_test)

    if targeted:
        loss = -loss
    return loss


def main():
    # Prepare dataset
    # For ImageNet, please specify the file path manually
    print("Loading dataset...")
    num_classes = 10
    x_train = get_dataset()

    test_dataset = datasets.CIFAR10(
        root="../dataset/cifar-10/",
        train=False,
        download=True,
        transform=None,
    )

    x_test, y_test = _normalize(test_dataset.data), _one_hot(
        np.array(test_dataset.targets), num_classes
    )
    x_train, y_train = (
        _normalize(x_train.data),
        _one_hot(np.array(x_train.targets), num_classes),
    )

    # Build model
    print("Building model...")
    key = random.PRNGKey(0)
    b_std, W_std = np.sqrt(0.18), np.sqrt(
        1.76
    )  # Standard deviation of initial biases and weights
    init_fn, apply_fn, kernel_fn = surrogate_fn(
        args.model_type, W_std, b_std, num_classes
    )
    apply_fn = jit(apply_fn)
    kernel_fn = jit(kernel_fn, static_argnums=(2,))

    # grads_fn: a callable that takes an input tensor and a loss function,
    # and returns the gradient w.r.t. an input tensor.
    grads_fn = jit(grad(adv_loss, argnums=0), static_argnums=(4, 5, 7))

    # Generate Neural Tangent Generalization Attacks (NTGA)
    print("Generating NTGA....")
    x_train_adv = []
    y_train_adv = []
    epoch = int(x_train.shape[0] / args.block_size)
    for idx in tqdm(range(epoch)):
        _x_train = x_train[idx * args.block_size: (idx + 1) * args.block_size]
        _y_train = y_train[idx * args.block_size: (idx + 1) * args.block_size]
        _x_train_adv = projected_gradient_descent(
            model_fn=model_fn,
            kernel_fn=kernel_fn,
            grads_fn=grads_fn,
            x_train=_x_train,
            y_train=_y_train,
            x_test=x_test,
            y_test=y_test,
            t=args.t,
            loss="cross-entropy",
            eps=args.eps,
            eps_iter=eps_iter,
            nb_iter=args.nb_iter,
            clip_min=0,
            clip_max=1,
            batch_size=args.batch_size,
        )

        x_train_adv.append(_x_train_adv)
        y_train_adv.append(_y_train)

        # Performance of clean and poisoned data
        _, y_pred = model_fn(
            kernel_fn=kernel_fn, x_train=x_train, x_test=x_test, y_train=y_train
        )
        print("Clean Acc: {:.2f}".format(accuracy(y_pred, y_test)))
        _, y_pred = model_fn(
            kernel_fn=kernel_fn,
            x_train=x_train_adv[-1],
            x_test=x_test,
            y_train=y_train_adv[-1],
        )
        print("NTGA Robustness: {:.2f}".format(accuracy(y_pred, y_test)))

    # Save poisoned data
    x_train_adv = np.concatenate(x_train_adv, axis=0)
    y_train_adv = np.concatenate(y_train_adv, axis=0)

    if args.dataset == "c10":
        x_train_adv = x_train_adv.reshape(-1, 32, 32, 3)
    else:
        raise ValueError("Please specify the image size manually.")

    def export_poison(args, advinputs, trainset):
        directory = "dataset/ntga_poisons/"
        path = os.path.join(directory, args.dataset)
        if not os.path.exists(path):
            os.makedirs(path)

        def to_PIL(image_ndarray):
            image_ndarray = image_ndarray * 255
            image_PIL = PIL.Image.fromarray(image_ndarray)
            return image_PIL

        def _save_image(input, label, idx, location, train=True):
            filename = os.path.join(location, str(idx) + ".png")
            adv_input = advinputs[idx]
            to_PIL(adv_input).save(filename)

        os.makedirs(os.path.join(path, "data"), exist_ok=True)
        for input, label, idx in tqdm(trainset, desc="Poisoned dataset generation"):
            _save_image(
                input, label, idx, location=os.path.join(path, "data"), train=True
            )
        print("Dataset fully exported.")

    export_poison(args, x_train_adv, x_train)


if __name__ == "__main__":
    main()
