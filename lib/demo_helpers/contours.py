#!/usr/bin/env python3
# -*- coding: utf-8 -*-


# ---------------------------------------------------------------------------------------------------------------------
# %% Imports

import cv2
import numpy as np

# For type hints
from numpy import ndarray


# %% Data types


class MaskContourData:
    """
    Helper class used to manage hierarchical contouring data
    Hierarchical meaning that the data can properly represent 'shapes with holes',
    or even 'shapes with holes that have shapes inside them' etc.
    """

    contour_norms_list: list[ndarray]
    area_norms_list: ndarray
    mask_hw: tuple[int, int]

    # Array which holds information about how contours are related:
    # Each row (corresponds to each contour) holds: [next_idx, prev_idx, first_child_idx, parent_idx]
    # See: https://docs.opencv.org/3.4/d9/d8b/tutorial_py_contours_hierarchy.html
    hierarchy: ndarray

    # Array which stores boolean values indicating whether a contour represents
    # an 'island' (white regions of a mask) or a 'hole' (black regions)
    is_island_list: ndarray

    # .................................................................................................................

    def __init__(self, mask_binary_uint8, external_masks_only=False):

        # Force a single-channel channel mask, in case we don't get one (assuming HxWxC shape!)
        if mask_binary_uint8.ndim == 3:
            assert mask_binary_uint8.shape[2] < mask_binary_uint8[1], "Mask error, expecting shape: HxWxC"
            mask_binary_uint8 = mask_binary_uint8[:, :, 0]

        # Generate outlines from the segmentation mask
        mode = cv2.RETR_EXTERNAL if external_masks_only else cv2.RETR_TREE
        contours_px_list, hierarchy = cv2.findContours(mask_binary_uint8, mode, cv2.CHAIN_APPROX_SIMPLE)
        max_area = mask_binary_uint8.shape[0] * mask_binary_uint8.shape[1]
        area_norms_list = np.float32([cv2.contourArea(c, oriented=False) for c in contours_px_list]) / max_area

        # Normalize contours for storage (now that we calculated area)
        contours_norm_list = normalize_contours(contours_px_list, mask_binary_uint8.shape)

        # Remove first empty dimension and force hierarchy data to be a numpy array, even if there is no data
        hierarchy = hierarchy[0] if (hierarchy is not None) else np.empty((0, 4), dtype=np.int32)

        # Figure out which contours should be filled in black vs. white (i.e. holes vs. islands)
        num_contours = len(contours_norm_list)
        is_island_list = np.zeros(num_contours, bool)
        for idx in range(num_contours):

            # Figure out whether the contour is an island or hole
            # -> Top-most contours (no parent) are always islands. Next child is hole, next-next child is island etc.
            parent_idx = int(hierarchy[idx, 3])
            is_topmost = parent_idx == -1
            is_island_list[idx] = True if is_topmost else (not is_island_list[parent_idx])

        # Store results
        self.contour_norms_list = contours_norm_list
        self.hierarchy = hierarchy
        self.area_norms_list = area_norms_list
        self.is_island_list = is_island_list
        self.mask_hw = mask_binary_uint8.shape[0:2]

    # .................................................................................................................

    def __len__(self):
        return len(self.contour_norms_list)

    # .................................................................................................................

    def index_iter(self):
        """
        Helper used to iterator over each contour index & corresponding parent index
        Use as:
            for index, parent_index in self.index_iter():
                is_topmost = parent_index == -1
                etc.
                pass
        """

        for idx in range(len(self.contour_norms_list)):
            parent_idx = int(self.hierarchy[idx, 3])
            yield idx, parent_idx

        return

    # .................................................................................................................

    def draw_mask(self, mask_shape_or_hw=None, filter_array=None) -> ndarray:
        """Function used to draw a mask image from a hierarchical listing of contour data"""

        # Figure out normalized-to-pixel sizing factors
        mask_shape = mask_shape_or_hw if mask_shape_or_hw is not None else self.mask_hw
        mask_h, mask_w = mask_shape[0:2]
        xy_norm_to_px_factor = np.float32((mask_w - 1, mask_h - 1))

        # If no filter is given, then simply allow all contours to be drawn
        if filter_array is None:
            filter_array = (True for _ in range(len(self.contour_norms_list)))

        # Draw each (valid, island) contour in sequence to reconstruct mask image
        mask_1ch = np.zeros((mask_h, mask_w), dtype=np.uint8)
        for contour, is_island, is_valid in zip(self.contour_norms_list, self.is_island_list, filter_array):

            # Skip invalid contours (for example, filtered by being too small)
            if not is_valid:
                continue

            # Convert to pixel units for drawing
            color = 255 if is_island else 0
            xy_px = np.int32(np.round(np.float32(contour) * xy_norm_to_px_factor))
            cv2.fillPoly(mask_1ch, [xy_px], color, cv2.LINE_8)
            if not is_island:
                # Due to way 'fillPoly' works, we need additional outline around holes,
                # in order to match the original mask that produced the contours!
                cv2.polylines(mask_1ch, [xy_px], True, 255)

        return mask_1ch

    # .................................................................................................................

    def get_bounding_box(self):
        """
        Function used to get the outer-most xy coordinates of all the contours
        Returns:
            top_left_xy_norm, bottom_right_xy_norm
        """

        # If we have no contours, consider the whole area as being bounded
        if len(self) == 0:
            return np.float32((0, 0)), np.float32((1, 1))

        # Find min/max of every top-most contour (child contours are irrelevant)
        topmost_contours = []
        for idx, parent_idx in self.index_iter():
            is_topmost = parent_idx == -1
            if is_topmost:
                topmost_contours.append(self.contour_norms_list[idx])

        tl_xy_norm = np.min([np.min(c.squeeze(1), axis=0) for c in topmost_contours], axis=0)
        br_xy_norm = np.max([np.max(c.squeeze(1), axis=0) for c in topmost_contours], axis=0)

        return tl_xy_norm, br_xy_norm

    # .................................................................................................................

    def filter_by_size_thresholds(self, hole_size_threshold=0, island_size_threshold=2) -> ndarray:
        """
        Create filtering array (used when drawing) that excludes overly small contours
        Assuming size thresholds are given between 0 and 100
        (e.g. a hole threshold of 100 would mean that we remove ALL holes)
        """

        # Convert thresholds (assumed to be 0 to 1) into reasonable range of area values
        # -> Squaring should give 'area-like' threshold behavior
        # -> For some reason, raising to power 8 gives more intuitive feeling behavior... not sure why
        hole_area_thresh = (hole_size_threshold / 100.0) ** 8
        island_area_thresh = (island_size_threshold / 100.0) ** 8

        # Update per-contour validity based on thresholding
        is_valid_list = [True] * len(self)
        for idx, area in enumerate(self.area_norms_list):

            # First check if parent is valid (otherwise, child is invalid by default)
            parent_idx = int(self.hierarchy[idx, 3])
            is_topmost = parent_idx == -1
            is_parent_valid = True if is_topmost else is_valid_list[parent_idx]

            # Decide if the contour is 'valid' based on sizing thresholds & parent status
            is_valid = is_parent_valid
            if is_valid:
                is_island = self.is_island_list[idx]
                is_small_hole = (not is_island) and (area < hole_area_thresh)
                is_small_island = is_island and (area < island_area_thresh)
                is_valid = (not is_small_island) and (not is_small_hole)

            is_valid_list[idx] = is_valid

        return is_valid_list

    # .................................................................................................................

    def filter_by_largest(self):
        """
        Create filtering array that excludes all but the largest contour
        """

        # Find largest contour by area and create filtering area to draw only this contour
        largest_idx = np.argmax(self.area_norms_list)
        filter_array = np.zeros(len(self), dtype=bool)
        filter_array[largest_idx] = True

        # Sanity check... largest is always an outer-most mask, right?
        assert self.hierarchy[largest_idx, 3] == -1, "Largest contour is not an outer-most contour?!"

        # Include all child contours, if parent was included
        for idx, parent_idx in self.index_iter():

            # Ignore contours without parent
            is_topmost = parent_idx == -1
            if is_topmost:
                continue

            filter_array[idx] = filter_array[parent_idx]

        return filter_array

    # .................................................................................................................

    def filter_by_containing_xy(self, xy_norm):
        """
        Create filtering array that excludes all contours not containing the given xy coord (if any)
        """

        filter_array = np.zeros(len(self), dtype=bool)
        for idx, parent_idx in self.index_iter():

            # Ignore non-topmost contours
            is_topmost = parent_idx == -1
            if is_topmost:
                # Record contour index if it contains the target point
                contains_xy = cv2.pointPolygonTest(self.contour_norms_list[idx], xy_norm, False) > 0

            else:
                # 'Child' case: Considered to contain xy if parent contains it
                contains_xy = filter_array[parent_idx]

            # Record parent/child results
            filter_array[idx] = contains_xy

        return filter_array


# ---------------------------------------------------------------------------------------------------------------------
# %% Functions


def get_largest_contour_from_mask(
    mask_binary_uint8,
    minimum_contour_area_norm=None,
    normalize=True,
    simplification_eps=None,
) -> [bool, ndarray]:
    """
    Helper used to get only the largest contour (by area) from a a given binary mask image.

    Inputs:
        mask_uint8 - A uint8 numpy array where bright values indicate areas to be masked
        minimum_contour_area_norm - (None or number 0-to-1) Any contour with area making up less
                                    than this percentage of the mask will be excluded from the output
        normalize - If true, contour xy coords. will be in range (0.0 to 1.0), otherwise they're in pixel coords
        simplification_eps - Value indicating how much to simplify the resulting contour. Larger values lead
                             to greater simplification (value is roughly a 'pixel' unit). Set to None to disable

    Returns:
        ok_contour (boolean), largest_contour
    """

    # Initialize outputs
    ok_contour = False
    largest_contour = None

    # Get all contours, bail if we don't get any
    ok_contour, contours_list = get_contours_from_mask(mask_binary_uint8, normalize=False)
    if not ok_contour:
        return ok_contour, largest_contour

    # Grab largest contour by area
    contour_areas = [cv2.contourArea(each_contour) for each_contour in contours_list]
    idx_of_largest_contour = np.argmax(contour_areas)
    largest_contour = contours_list[idx_of_largest_contour]
    if minimum_contour_area_norm is not None:
        mask_h, mask_w = mask_binary_uint8.shape[0:2]
        max_area = mask_h * mask_w
        min_area_px = int(max_area * minimum_contour_area_norm)
        largest_area = contour_areas[idx_of_largest_contour]
        ok_contour = largest_area >= min_area_px
        if not ok_contour:
            largest_contour = None
            return ok_contour, largest_contour

    # Simplify if needed
    need_to_simplify = simplification_eps is not None
    if need_to_simplify:
        largest_contour = simplify_contour_px(largest_contour, simplification_eps)

    # Apply normalization if needed
    # (couldn't apply earlier, since we need to use pixel coords for area calculations!)
    if normalize:
        mask_h, mask_w = mask_binary_uint8.shape[0:2]
        norm_scale_factor = 1.0 / np.float32((mask_w - 1, mask_h - 1))
        largest_contour = largest_contour * norm_scale_factor

    return ok_contour, largest_contour.squeeze(1)


# .....................................................................................................................


def get_contours_from_mask(
    mask_binary_uint8,
    minimum_contour_area_norm=0,
    normalize=True,
) -> [bool, tuple]:
    """
    Function which takes in a binary black & white mask and returns contours around each independent 'blob'
    within the mask. Note that only the external-most contours are returned, without holes!

    Inputs:
        mask_binary_uint8 - A uint8 numpy array where bright values indicate areas to be masked
        minimum_contour_area_norm - (None or number 0-to-1) Any contour with area making up less
                                    than this percentage of the mask will be excluded from the output
        normalize - If true, contour xy coords. will be in range (0.0 to 1.0), otherwise they're in pixel coords

    Returns:
        have_contours (boolean), mask_contours_as_tuple
    """

    # Initialize outputs
    have_contours = False
    mask_contours_list = []

    # Generate outlines from the segmentation mask
    mask_contours_list, _ = cv2.findContours(mask_binary_uint8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Bail if we have no contours from the mask
    have_contours = len(mask_contours_list) > 0
    if not have_contours:
        return have_contours, tuple(mask_contours_list)

    # Filter out small contours, if needed
    if minimum_contour_area_norm > 0:
        mask_h, mask_w = mask_binary_uint8.shape[0:2]
        max_area = mask_h * mask_w
        min_area_px = int(max_area * minimum_contour_area_norm)
        mask_contours_list = [cont for cont in mask_contours_list if cv2.contourArea(cont) > min_area_px]
        have_contours = len(mask_contours_list) > 0

    # Normalize xy coords if needed
    if normalize:
        mask_contours_list = normalize_contours(mask_contours_list, mask_binary_uint8.shape)

    # Remove any 1 or 2 point 'contours' since these aren't valid shapes
    mask_contours_list = [c for c in mask_contours_list if len(c) > 2]

    return have_contours, tuple(mask_contours_list)


# .....................................................................................................................


def get_contours_containing_xy(contours_list, xy) -> [bool, list]:
    """Helper used to filter out contours that do not contain the given xy coordinate"""
    filtered_list = [contour for contour in contours_list if cv2.pointPolygonTest(contour, xy, False) > 0]
    have_results = len(filtered_list) > 0
    return have_results, filtered_list


# .....................................................................................................................


def get_largest_contour(contours_list, reference_shape=None) -> [bool, ndarray]:
    """
    Helper used to filter out only the largest contour from a list of contours

    If the given contours use normalized coordinates, then the 'largest' calculation can be
    incorrect, due to uneven width/height scaling. In these cases, a reference frame shape
    can be given, which will be used to scale the normalized values appropriately
    before determining which is the largest.

    Returns:
        index of the largest contour, largest_contour
    """

    # Use aspect-ratio adjusted area calculation, if possible
    area_calc = lambda contour: cv2.contourArea(contour)
    if reference_shape is not None:
        frame_h, frame_w = reference_shape[0:2]
        scale_factor = np.float32((frame_w - 1, frame_h - 1))
        area_calc = lambda contour: cv2.contourArea(contour * scale_factor)

    # Grab largest contour by area
    contour_areas = [area_calc(contour) for contour in contours_list]
    idx_of_largest_contour = np.argmax(contour_areas)
    largest_contour = contours_list[idx_of_largest_contour]

    return idx_of_largest_contour, largest_contour


# .....................................................................................................................


def simplify_contour_px(contour_px, simplification_eps=1.0, scale_to_perimeter=False) -> ndarray:
    """
    Function used to simplify a contour, without completely altering the overall shape
    (as compared to finding the convex hull, for example). Uses the Ramer–Douglas–Peucker algorithm

    Inputs:
        contour_px - A single contour to be simplified (from opencv findContours() function), must be in px units!
        simplification_eps - Value that determines how 'simple' the result should be. Larger values
                             result in more heavily approximated contours
        scale_to_perimeter - If True, the eps value is scaled by the contour perimeter before performing
                             the simplification. Otherwise, the eps value is used as-is

    Returns:
        simplified_contour
    """

    # Decide whether to use perimeter scaling for approximation value
    epsilon = simplification_eps
    if scale_to_perimeter:
        epsilon = cv2.arcLength(contour_px, closed=True) * simplification_eps

    return cv2.approxPolyDP(contour_px, epsilon, closed=True)


# .....................................................................................................................


def normalize_contours(contours_px_list, frame_shape):
    """Helper used to normalize contour data, according to a given frame shape (i.e. [height, width]"""

    frame_h, frame_w = frame_shape[0:2]
    norm_scale_factor = 1.0 / np.float32((frame_w - 1, frame_h - 1))

    return [np.float32(contour) * norm_scale_factor for contour in contours_px_list]


# .....................................................................................................................


def pixelize_contours(contours_norm_list, frame_shape):
    """Helper used to convert normalized contours to pixel coordinates"""

    frame_h, frame_w = frame_shape[0:2]
    scale_factor = np.float32((frame_w - 1, frame_h - 1))

    return [np.int32(np.round(contour * scale_factor)) for contour in contours_norm_list]
