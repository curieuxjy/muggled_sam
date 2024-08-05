#!/usr/bin/env python3
# -*- coding: utf-8 -*-


# ---------------------------------------------------------------------------------------------------------------------
# %% Imports

import cv2
import numpy as np

import torch
import torch.nn as nn

from .components.hiera_model import HieraModel
from .components.imgenc_components import HalfStepPatchEmbed, WindowedPositionEncoding, OutputProjection
from .components.shared import Conv1x1Layer

# For type hints
from torch import Tensor
from numpy import ndarray


# ---------------------------------------------------------------------------------------------------------------------
# %% Classes


class SAMV2ImageEncoder(nn.Module):
    """
    Simplified implementation of the image encoder from:
        "SAM 2: Segment Anything in Images and Videos"
        By: Nikhila Ravi, Valentin Gabeur, Yuan-Ting Hu, Ronghang Hu, Chaitanya Ryali, Tengyu Ma,
        Haitham Khedr, Roman Rädle, Chloe Rolland, Laura Gustafson, Eric Mintun, Junting Pan,
        Kalyan Vasudev Alwala, Nicolas Carion, Chao-Yuan Wu, Ross Girshick,
        Piotr Dollár, Christoph Feichtenhofer
        @ https://ai.meta.com/research/publications/sam-2-segment-anything-in-images-and-videos/

    The original code can be found here:
    https://github.com/facebookresearch/segment-anything-2/blob/main/sam2/modeling/backbones/image_encoder.py

    This implementation re-arranges various components and formats it's outputs differently compared
    to the original code. There is also far less flexibility in configuration here
    (only supporting final SAMV2 configs).
    """

    rgb_offset = [123.675, 116.28, 103.53]
    rgb_stdev = [58.395, 57.12, 57.375]

    # .................................................................................................................

    def __init__(
        self,
        features_per_token=112,
        output_channels=256,
        num_heads=2,
        blocks_per_stage=(2, 3, 16, 3),
        global_attn_spacing_per_stage=(None, None, 4, None),
        window_size_per_stage=(8, 4, 14, 17),
        base_patch_grid_hw=(14, 14),
        patch_size_px=7,
    ):

        # Inherit from parent
        super().__init__()

        # Create patch embedding to create patch tokens along with positional encoder
        self._patch_size_px = patch_size_px
        self.patch_embed = HalfStepPatchEmbed(features_per_token, patch_size_px)

        # Set up position encoding applied to patch embedding tokens
        first_stage_window_size = window_size_per_stage[0]
        base_wintile_hw = (first_stage_window_size, first_stage_window_size)
        self.posenc = WindowedPositionEncoding(features_per_token, base_patch_grid_hw, base_wintile_hw)

        # Set up hierarchical image encoder model
        self.trunk = HieraModel(
            features_per_token,
            num_heads,
            blocks_per_stage,
            window_size_per_stage,
            global_attn_spacing_per_stage,
        )

        # Create output projection model which follows the trunk model
        features_per_stage = self.trunk.get_features_per_stage()
        self.output_projection = OutputProjection(output_channels, features_per_stage)

        # Embedding added to encoded image features (when not using memory encoding?)
        self.no_mem_embed = torch.nn.Parameter(torch.empty(1, 1, output_channels))

        # New to version-2, used to process pass-thru features sent to the mask decoder
        self.proj_x4 = Conv1x1Layer(output_channels, output_channels // 8)
        self.proj_x2 = Conv1x1Layer(output_channels, output_channels // 4)

        # Store image scaling values
        self.register_buffer("mean_rgb", torch.tensor(self.rgb_offset).view(-1, 1, 1), persistent=False)
        self.register_buffer("stdev_scale_rgb", 1.0 / torch.tensor(self.rgb_stdev).view(-1, 1, 1), persistent=False)

    # .................................................................................................................

    def forward(self, image_tensor_bchw: Tensor) -> tuple[list[Tensor], list[Tensor]]:
        """
        Encodes an image into multi-resolution feature maps, and also produces
        a corresponding set of positional embedding maps.

        Returns:
            [lowres_features, features_x2, features_x4], [lowres_posenc, posenc_x2, posenc_x4]
        """

        # Prepare image tokens for transformer
        patch_tokens_bhwc = self.patch_embed(image_tensor_bchw)
        patch_tokens_bhwc = self.posenc(patch_tokens_bhwc)

        # Forward through backbone
        multires_tokens_list = self.trunk(patch_tokens_bhwc)
        features_list, posembed_list = self.output_projection(multires_tokens_list)

        # For clarity
        lowres_features, hires_features_x2, hires_features_x4 = features_list

        # Further process high-res features
        hires_features_x4 = self.proj_x4(hires_features_x4)
        hires_features_x2 = self.proj_x2(hires_features_x2)

        # Add no-memory embedding to lowest-res feature map (that is used), see:
        # https://github.com/facebookresearch/segment-anything-2/blob/0e78a118995e66bb27d78518c4bd9a3e95b4e266/sam2/sam2_image_predictor.py#L142
        lowres_features += self.no_mem_embed.squeeze(0).unsqueeze(-1).unsqueeze(-1)

        # Re-bundle features for easier handling (note, this is reversed order from original!)
        features_list = [lowres_features, hires_features_x2, hires_features_x4]

        # Skipping '_prepare_backbone_features' step from original code, see:
        # https://github.com/facebookresearch/segment-anything-2/blob/main/sam2/modeling/sam2_base.py#L477C9-L477C35

        # Skipping a strange looking step (maybe important?), see:
        # https://github.com/facebookresearch/segment-anything-2/blob/0e78a118995e66bb27d78518c4bd9a3e95b4e266/sam2/sam2_image_predictor.py#L146-L149
        # -> Seems to just be re-arranging features back into image-like shape, after earlier flatten
        # -> Flatten step seems to be done just for adding no_mem_embed?

        return features_list, posembed_list

    # .................................................................................................................

    def prepare_image(
        self,
        image_bgr: ndarray,
        max_side_length=1024,
        use_square_sizing=False,
        pad_to_square=False,
    ) -> Tensor:
        """
        Helper used to convert opencv-formatted images (e.g. from loading: cv2.imread(path_to_image)
        into the format needed by the image encoder model (includes scaling and RGB normalization steps)
        Returns:
            image_as_tensor_bchw
        """

        # Figure out scaling factor to get target side length
        img_h, img_w = image_bgr.shape[0:2]
        largest_side = max(img_h, img_w)
        scale_factor = max_side_length / largest_side

        # Force sizing to multiples of a specific tiling size
        tiling_size = self.get_image_tiling_size_constraint()
        if use_square_sizing:
            scaled_side = int(np.ceil(largest_side * scale_factor / tiling_size)) * tiling_size
            scaled_h = scaled_w = scaled_side
        else:
            scaled_h = int(np.ceil(img_h * scale_factor / tiling_size)) * tiling_size
            scaled_w = int(np.ceil(img_w * scale_factor / tiling_size)) * tiling_size

        # Scale RGB image to correct size and re-order from HWC to BCHW (with batch of 1)
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        scaled_hwc = cv2.resize(image_rgb, dsize=(scaled_w, scaled_h))
        scaled_bchw = np.expand_dims(np.transpose(scaled_hwc, (2, 0, 1)), 0)

        # Move the image over to the pytorch for final pre-processing steps
        device, dtype = self.mean_rgb.device, self.mean_rgb.dtype
        image_tensor_bchw = torch.tensor(scaled_bchw, device=device, dtype=dtype)
        image_tensor_bchw = (image_tensor_bchw - self.mean_rgb) * self.stdev_scale_rgb

        # The original SAM implementation padded the short side of the image to form a square
        # -> This results in more processing and isn't required in this implementation!
        if pad_to_square:
            pad_left, pad_top = 0, 0
            pad_bottom = max_side_length - scaled_h
            pad_right = max_side_length - scaled_w
            image_tensor_bchw = nn.functional.pad(image_tensor_bchw, (pad_left, pad_right, pad_top, pad_bottom))

        return image_tensor_bchw

    # .................................................................................................................

    def get_image_tiling_size_constraint(self) -> int:
        """
        Due to the hierarchical structure of the image encoder, input images
        must adhere to certain sizing constraints. In particular, input images
        must be multiples of the patch sizing. Additionally, after patch embedding,
        the patch grid shape (i.e. number of patch tokens in height/width) must be
        divisible by a factor of 2, 3 times, in order to support hierarchical downsampling.

        To make things more confusing, the patch embedding uses half-steps, so it
        produces double the number of tokens expected based on the patch sizing alone.

        This function computes the required tiling size constraint used when scaling
        input images before processing. All images must be integer multiples of this size!
        """

        # Calculate the tiling size of the patch embedding
        # -> For the default patch size of 7, it has a tiling size of 4 due to being half-stepped
        # -> For example, a 768x1024 image will produce a patch grid size: 192x256 (i.e. 4x smaller)
        patch_tiling_size = (self._patch_size_px + 1) // 2

        # The patch grid must itself be sized to allow for repeated 2x downsamples for hiera model
        num_downsamples = 3
        to_multiples_requirement = patch_tiling_size * (2**num_downsamples)

        return int(to_multiples_requirement)

    # .................................................................................................................
