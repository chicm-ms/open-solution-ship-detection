import logging
import os
import pathlib
import random
import sys
import time
from itertools import chain
from collections import Iterable
import math

from deepsense import neptune
import numpy as np
import pandas as pd
import torch
from PIL import Image
import matplotlib.pyplot as plt
from attrdict import AttrDict
from tqdm import tqdm
from pycocotools import mask as cocomask
from sklearn.model_selection import BaseCrossValidator
from sklearn.externals import joblib
from steppy.base import BaseTransformer
import yaml
from imgaug import augmenters as iaa
import imgaug as ia
from scipy import ndimage as ndi

NEPTUNE_CONFIG_PATH = str(pathlib.Path(__file__).resolve().parents[1] / 'configs' / 'neptune.yaml')


def read_params(ctx):
    if ctx.params.__class__.__name__ == 'OfflineContextParams':
        params = read_yaml().parameters
    else:
        params = ctx.params
    return params


def read_yaml(fallback_file=NEPTUNE_CONFIG_PATH):
    with open(fallback_file) as f:
        config = yaml.load(f)
    return AttrDict(config)


def init_logger():
    logger = logging.getLogger('ships-detection')
    logger.setLevel(logging.INFO)
    message_format = logging.Formatter(fmt='%(asctime)s %(name)s >>> %(message)s',
                                       datefmt='%Y-%m-%d %H-%M-%S')

    # console handler for validation info
    ch_va = logging.StreamHandler(sys.stdout)
    ch_va.setLevel(logging.INFO)

    ch_va.setFormatter(fmt=message_format)

    # add the handlers to the logger
    logger.addHandler(ch_va)

    return logger


def get_logger():
    return logging.getLogger('ships-detection')


def decompose(labeled):
    nr_true = labeled.max()
    masks = []
    for i in range(1, nr_true + 1):
        msk = labeled.copy()
        msk[msk != i] = 0.
        msk[msk == i] = 255.
        masks.append(msk)

    if not masks:
        return [labeled]
    else:
        return masks


def create_submission(image_ids, predictions):
    output = []
    for image_id, mask in zip(image_ids, predictions):
        for label_nr in range(1, mask.max() + 1):
            mask_label = mask == label_nr
            rle_encoded = ' '.join(str(rle) for rle in run_length_encoding(mask_label))
            output.append([image_id, rle_encoded])
        if mask.max() == 0:
            output.append([image_id, None])

    submission = pd.DataFrame(output, columns=['ImageId', 'EncodedPixels'])
    return submission


def encode_rle(predictions):
    return [run_length_encoding(mask) for mask in predictions]


def read_masks(masks_filepaths):
    masks = []
    for mask_filepath in tqdm(masks_filepaths):
        mask = joblib.load(mask_filepath)[0]
        masks.append(mask)
    return masks


def read_masks_from_csv(image_ids, solution_file_path, image_sizes):
    solution = pd.read_csv(solution_file_path)
    masks = []
    for image_id, image_size in zip(image_ids, image_sizes):
        image_id_pd = image_id + ".jpg"
        mask = get_overlayed_mask(solution.query('ImageId == @image_id_pd'), image_size, labeled=True)
        masks.append(mask)
    return masks


def read_gt_subset(annotation_file_path, image_ids):
    solution = pd.read_csv(annotation_file_path)
    return solution.query('ImageId in @image_ids')


def get_overlayed_mask(image_annotation, size, labeled=False):
    mask = np.zeros(size, dtype=np.uint8)
    if image_annotation['EncodedPixels'].any():
        for i, row in image_annotation.reset_index(drop=True).iterrows():
            if labeled:
                label = i + 1
            else:
                label = 1
            mask += label * run_length_decoding(row['EncodedPixels'], size)
    return mask


def read_images(filepaths):
    images = []
    for filepath in filepaths:
        image = np.array(Image.open(filepath))
        images.append(image)
    return images


def run_length_encoding(x):
    # https://www.kaggle.com/c/data-science-bowl-2018/discussion/48561#
    bs = np.where(x.T.flatten())[0]

    rle = []
    prev = -2
    for b in bs:
        if (b > prev + 1): rle.extend((b + 1, 0))
        rle[-1] += 1
        prev = b

    if len(rle) != 0 and rle[-1] + rle[-2] == x.size:
        rle[-2] = rle[-2] - 1

    return rle


def run_length_decoding(mask_rle, shape):
    """
    Based on https://www.kaggle.com/msl23518/visualize-the-stage1-test-solution and modified
    Args:
        mask_rle: run-length as string formatted (start length)
        shape: (height, width) of array to return

    Returns:
        numpy array, 1 - mask, 0 - background

    """
    s = mask_rle.split()
    starts, lengths = [np.asarray(x, dtype=int) for x in (s[0:][::2], s[1:][::2])]
    starts -= 1
    ends = starts + lengths
    img = np.zeros(shape[1] * shape[0], dtype=np.uint8)
    for lo, hi in zip(starts, ends):
        img[lo:hi] = 1
    return img.reshape((shape[1], shape[0])).T


def generate_metadata(train_images_dir, masks_overlayed_dir, test_images_dir, annotation_file_name):
    metadata = {}
    annotations = pd.read_csv(annotation_file_name)
    for filename in tqdm(os.listdir(train_images_dir)):
        image_filepath = os.path.join(train_images_dir, filename)
        mask_filepath = os.path.join(masks_overlayed_dir, filename.split('.')[0])
        image_id = filename.split('.')[0]
        number_of_ships = get_number_of_ships(annotations.query('ImageId == @filename'))

        metadata.setdefault('file_path_image', []).append(image_filepath)
        metadata.setdefault('file_path_mask', []).append(mask_filepath)
        metadata.setdefault('is_train', []).append(1)
        metadata.setdefault('id', []).append(image_id)
        metadata.setdefault('number_of_ships', []).append(number_of_ships)

    for filename in tqdm(os.listdir(test_images_dir)):
        image_filepath = os.path.join(test_images_dir, filename)
        image_id = filename.split('.')[0]

        metadata.setdefault('file_path_image', []).append(image_filepath)
        metadata.setdefault('file_path_mask', []).append(None)
        metadata.setdefault('is_train', []).append(0)
        metadata.setdefault('id', []).append(image_id)
        metadata.setdefault('number_of_ships', []).append(None)

    return pd.DataFrame(metadata)


def get_number_of_ships(image_annotations):
    if image_annotations['EncodedPixels'].any():
        return len(image_annotations)
    else:
        return 0


def sigmoid(x):
    return 1. / (1 + np.exp(-x))


def softmax(X, theta=1.0, axis=None):
    """
    https://nolanbconaway.github.io/blog/2017/softmax-numpy
    Compute the softmax of each element along an axis of X.

    Parameters
    ----------
    X: ND-Array. Probably should be floats.
    theta (optional): float parameter, used as a multiplier
        prior to exponentiation. Default = 1.0
    axis (optional): axis to compute values along. Default is the
        first non-singleton axis.

    Returns an array the same size as X. The result will sum to 1
    along the specified axis.
    """

    # make X at least 2d
    y = np.atleast_2d(X)

    # find axis
    if axis is None:
        axis = next(j[0] for j in enumerate(y.shape) if j[1] > 1)

    # multiply y against the theta parameter,
    y = y * float(theta)

    # subtract the max for numerical stability
    y = y - np.expand_dims(np.max(y, axis=axis), axis)

    # exponentiate y
    y = np.exp(y)

    # take the sum along the specified axis
    ax_sum = np.expand_dims(np.sum(y, axis=axis), axis)

    # finally: divide elementwise
    p = y / ax_sum

    # flatten if X was 1D
    if len(X.shape) == 1: p = p.flatten()

    return p


def from_pil(*images):
    images = [np.array(image) for image in images]
    if len(images) == 1:
        return images[0]
    else:
        return images


def to_pil(*images):
    images = [Image.fromarray((image).astype(np.uint8)) for image in images]
    if len(images) == 1:
        return images[0]
    else:
        return images


def make_apply_transformer(func, output_name='output', apply_on=None):
    class StaticApplyTransformer(BaseTransformer):
        def transform(self, *args, **kwargs):
            self.check_input(*args, **kwargs)

            if not apply_on:
                iterator = zip(*args, *kwargs.values())
            else:
                iterator = zip(*args, *[kwargs[key] for key in apply_on])

            output = []
            for func_args in tqdm(iterator, total=self.get_arg_length(*args, **kwargs)):
                output.append(func(*func_args))
            return {output_name: output}

        @staticmethod
        def check_input(*args, **kwargs):
            if len(args) and len(kwargs) == 0:
                raise Exception('Input must not be empty')

            arg_length = None
            for arg in chain(args, kwargs.values()):
                if not isinstance(arg, Iterable):
                    raise Exception('All inputs must be iterable')
                arg_length_loc = None
                try:
                    arg_length_loc = len(arg)
                except:
                    pass
                if arg_length_loc is not None:
                    if arg_length is None:
                        arg_length = arg_length_loc
                    elif arg_length_loc != arg_length:
                        raise Exception('All inputs must be the same length')

        @staticmethod
        def get_arg_length(*args, **kwargs):
            arg_length = None
            for arg in chain(args, kwargs.values()):
                if arg_length is None:
                    try:
                        arg_length = len(arg)
                    except:
                        pass
                if arg_length is not None:
                    return arg_length

    return StaticApplyTransformer()


def rle_from_binary(prediction):
    prediction = np.asfortranarray(prediction)
    return cocomask.encode(prediction)


def binary_from_rle(rle):
    return cocomask.decode(rle)


def get_segmentations(labeled):
    nr_true = labeled.max()
    segmentations = []
    for i in range(1, nr_true + 1):
        msk = labeled == i
        segmentation = rle_from_binary(msk.astype('uint8'))
        segmentation['counts'] = segmentation['counts'].decode("UTF-8")
        segmentations.append(segmentation)
    return segmentations


def get_crop_pad_sequence(vertical, horizontal):
    top = int(vertical / 2)
    bottom = vertical - top
    right = int(horizontal / 2)
    left = horizontal - right
    return (top, right, bottom, left)


def get_list_of_image_predictions(batch_predictions):
    image_predictions = []
    for batch_pred in batch_predictions:
        image_predictions.extend(list(batch_pred))
    return image_predictions


def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


class ImgAug:
    def __init__(self, augmenters):
        if not isinstance(augmenters, list):
            augmenters = [augmenters]
        self.augmenters = augmenters
        self.seq_det = None

    def _pre_call_hook(self):
        seq = iaa.Sequential(self.augmenters)
        seq = reseed(seq, deterministic=True)
        self.seq_det = seq

    def transform(self, *images):
        images = [self.seq_det.augment_image(image) for image in images]
        if len(images) == 1:
            return images[0]
        else:
            return images

    def __call__(self, *args):
        self._pre_call_hook()
        return self.transform(*args)


def get_seed():
    seed = int(time.time()) + int(os.getpid())
    return seed


def reseed(augmenter, deterministic=True):
    augmenter.random_state = ia.new_random_state(get_seed())
    if deterministic:
        augmenter.deterministic = True

    for lists in augmenter.get_children_lists():
        for aug in lists:
            aug = reseed(aug, deterministic=True)
    return augmenter


class KFoldBySortedValue(BaseCrossValidator):
    def __init__(self, n_splits=3, shuffle=False, random_state=None):
        self.n_splits = n_splits
        self.shuffle = shuffle
        self.random_state = random_state

    def _iter_test_indices(self, X, y=None, groups=None):
        n_samples = X.shape[0]
        indices = np.arange(n_samples)

        sorted_idx_vals = sorted(zip(indices, X), key=lambda x: x[1])
        indices = [idx for idx, val in sorted_idx_vals]

        for split_start in range(self.n_splits):
            split_indeces = indices[split_start::self.n_splits]
            yield split_indeces

    def get_n_splits(self, X=None, y=None, groups=None):
        return self.n_split


def plot_list(images=[], labels=[]):
    n_img = len(images)
    n_lab = len(labels)
    n = n_lab + n_img
    fig, axs = plt.subplots(1, n, figsize=(16, 12))
    for i, image in enumerate(images):
        axs[i].imshow(image)
        axs[i].set_xticks([])
        axs[i].set_yticks([])
    for j, label in enumerate(labels):
        axs[n_img + j].imshow(label, cmap='nipy_spectral')
        axs[n_img + j].set_xticks([])
        axs[n_img + j].set_yticks([])
    plt.show()


def clean_memory():
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def label(mask):
    labeled, nr_true = ndi.label(mask)
    return labeled


def generate_data_frame_chunks(meta, chunk_size):
    n_rows = meta.shape[0]
    chunk_nr = math.ceil(n_rows / chunk_size)
    for i in tqdm(range(chunk_nr)):
        meta_chunk = meta.iloc[i * chunk_size:(i + 1) * chunk_size]
        yield meta_chunk
