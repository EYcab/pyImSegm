"""
Framework for ellipse fitting

Copyright (C) 2014-2017 Jiri Borovec <jiri.borovec@fel.cvut.cz>
"""

import numpy as np
from scipy import ndimage, spatial
from skimage import morphology

from skimage.measure import fit as sk_fit
# from skimage.measure.fit import EllipseModel  # fix in future skimage>0.13.0
import segmentation.utils.drawing as tl_visu
import segmentation.descriptors as seg_fts
import segmentation.superpixels as seg_spx

INIT_MASK_BORDER = 50.
MIN_ELLIPSE_DAIM = 25.
MAX_FIGURE_SIZE = 14
SEGM_OVERLAP = 0.5
STRUC_ELEM_BG = 15
STRUC_ELEM_FG = 5


class EllipseModelSegm(sk_fit.EllipseModel):
    """Total least squares estimator for 2D ellipses.

    The functional model of the ellipse is::

        xt = xc + a*cos(theta)*cos(t) - b*sin(theta)*sin(t)
        yt = yc + a*sin(theta)*cos(t) + b*cos(theta)*sin(t)
        d = sqrt((x - xt)**2 + (y - yt)**2)

    where ``(xt, yt)`` is the closest point on the ellipse to ``(x, y)``. Thus
    d is the shortest distance from the point to the ellipse.

    The estimator is based on a least squares minimization. The optimal
    solution is computed directly, no iterations are required. This leads
    to a simple, stable and robust fitting method.

    The ``params`` attribute contains the parameters in the following order::

        xc, yc, a, b, theta

    Attributes
    ----------
    params : tuple
        Ellipse model parameters  `xc`, `yc`, `a`, `b`, `theta`.

    Example
    -------
    >>> params = 20, 30, 12, 16, np.deg2rad(30)
    >>> rr, cc = tl_visu.ellipse_perimeter(*params)
    >>> xy = np.array([rr, cc]).T
    >>> ellipse = EllipseModelSegm()
    >>> ellipse.estimate(xy)
    True
    >>> np.round(ellipse.params, 2)
    array([ 19.5 ,  29.5 ,  12.45,  16.52,   0.53])
    >>> xy = EllipseModelSegm().predict_xy(np.linspace(0, 2 * np.pi, 25), params)
    >>> ellipse = EllipseModelSegm()
    >>> ellipse.estimate(xy)
    True
    >>> np.round(ellipse.params, 2)
    array([ 20.  ,  30.  ,  12.  ,  16.  ,   0.52])
    >>> np.round(abs(ellipse.residuals(xy)), 5)
    array([ 0.,  0.,  0.,  0.,  0.,  0.,  0.,  0.,  0.,  0.,  0.,  0.,  0.,
            0.,  0.,  0.,  0.,  0.,  0.,  0.,  0.,  0.,  0.,  0.,  0.])
    >>> ellipse.params[2] += 2
    >>> ellipse.params[3] += 2
    >>> np.round(abs(ellipse.residuals(xy)))
    array([ 2.,  2.,  2.,  2.,  2.,  2.,  2.,  2.,  2.,  2.,  2.,  2.,  2.,
            2.,  2.,  2.,  2.,  2.,  2.,  2.,  2.,  2.,  2.,  2.,  2.])
    """

    def criterion(self, points, weights, labels,
                  table_p=((0.1, 0.9), (0.9, 0.1))):
        """ Determine residuals of data to model.

        Example
        -------
        >>> seg = np.zeros((10, 15), dtype=int)
        >>> r, c = np.meshgrid(range(seg.shape[1]), range(seg.shape[0]))
        >>> el = EllipseModelSegm()
        >>> el.params = [4, 7, 3, 6, np.deg2rad(10)]
        >>> weights = np.ones(seg.ravel().shape)
        >>> seg[4:5, 6:8] = 1
        >>> table_p = [[0.1, 0.9], [0.9, 0.1]]
        >>> el.criterion(np.array([r.ravel(), c.ravel()]).T, weights, seg.ravel(),
        ...              table_p)  # doctest: +ELLIPSIS
        87.888...
        >>> seg[2:7, 4:11] = 1
        >>> el.criterion(np.array([r.ravel(), c.ravel()]).T, weights, seg.ravel(),
        ...              table_p)  # doctest: +ELLIPSIS
        17.577...
        >>> seg[1:9, 1:14] = 1
        >>> el.criterion(np.array([r.ravel(), c.ravel()]).T, weights, seg.ravel(),
        ...              table_p)   # doctest: +ELLIPSIS
        -70.311...
        """
        assert len(points) == len(weights) == len(labels), \
            'different sizes for points %i and weights %i and labels %i' \
            % (len(points), len(weights), len(labels))
        table_p = np.array(table_p)
        assert table_p.shape[0] == 2, 'table shape %s' % repr(table_p.shape)
        assert np.max(labels) < table_p.shape[1], \
            'labels (%i) exceed the table %s' % \
            (np.max(labels), repr(table_p.shape))

        r_pos, c_pos = points[:, 0], points[:, 1]
        r_org, c_org, r_rad, c_rad, phi = self.params
        sin_phi, cos_phi = np.sin(phi), np.cos(phi)
        r, c = (r_pos - r_org), (c_pos - c_org)
        distances = ((r * cos_phi + c * sin_phi) / r_rad) ** 2 \
                    + ((r * sin_phi - c * cos_phi) / c_rad) ** 2
        inside = (distances <= 1)

        # import matplotlib.pyplot as plt
        # plt.imshow(labels.reshape((10, 15)), interpolation='nearest')
        # plt.contour(inside.reshape((10, 15)))

        table_q = - np.log(table_p)
        labels_in = labels[inside].astype(int)

        residual = np.sum(weights[labels_in] *
                          (table_q[0, labels_in] - table_q[1, labels_in]))

        return residual


def ransac_segm(points, model_class, points_all, weights, labels, table_prob,
                min_samples, residual_threshold=1, max_trials=100):
    """ Fit a model to points with the RANSAC (random sample consensus).

    Parameters
    ----------
    points : [list, tuple of] (N, D) array
        Data set to which the model is fitted, where N is the number of points
        points and D the dimensionality of the points.
        If the model class requires multiple input points arrays (e.g. source
        and destination coordinates of  ``skimage.transform.AffineTransform``),
        they can be optionally passed as tuple or list. Note, that in this case
        the functions ``estimate(*points)``, ``residuals(*points)``,
        ``is_model_valid(model, *random_data)`` and
        ``is_data_valid(*random_data)`` must all take each points array as
        separate arguments.
    model_class : object
        Object with the following object methods:

         * ``success = estimate(*points)``
         * ``residuals(*points)``

        where `success` indicates whether the model estimation succeeded
        (`True` or `None` for success, `False` for failure).
    min_samples : int float
        The minimum number of points points to fit a model to.
    residual_threshold : float
        Maximum distance for a points point to be classified as an inlier.
    max_trials : int, optional
        Maximum number of iterations for random sample selection.
    stop_sample_num : int, optional
        Stop iteration if at least this number of inliers are found.
    stop_residuals_sum : float, optional
        Stop iteration if sum of residuals is less than or equal to this
        threshold.


    Returns
    -------
    model : object
        Best model with largest consensus set.
    inliers : (N, ) array
        Boolean mask of inliers classified as ``True``.

    References
    ----------
    .. [1] "RANSAC", Wikipedia, http://en.wikipedia.org/wiki/RANSAC


    >>> seg = np.zeros((120, 150), dtype=int)
    >>> ell_params = 60, 75, 40, 65, np.deg2rad(30)
    >>> seg = add_overlap_ellipse(seg, ell_params, 1)
    >>> slic, points_all, labels = get_slic_points_labels(seg, slic_size=10,
    ...                                                   slic_regul=0.3)
    >>> points = prepare_boundary_points_ray_dist(seg, [(40, 90)], 2,
    ...                                           sel_bg=1, sel_fg=0)[0]
    >>> table_prob = [[0.01, 0.75, 0.95, 0.9], [0.99, 0.25, 0.05, 0.1]]
    >>> weights = np.bincount(slic.ravel())
    >>> ransac_model, _ = ransac_segm(points, EllipseModelSegm,
    ...                               points_all, weights, labels,
    ...                               table_prob, 0.6, 3, max_trials=15)
    >>> np.round(ransac_model.params[:4]).astype(int)
    array([60, 75, 40, 65])
    >>> np.round(ransac_model.params[4], 1)
    0.5
    """

    best_model = None
    best_inlier_num = 0
    best_model_fit = np.inf
    best_inliers = None

    if isinstance(min_samples, float):
        if not (0 <= min_samples <= 1):
            raise ValueError("`min_samples` as ration must be in range (0, 1)")
        min_samples = int(min_samples * len(points))
    if min_samples < 0:
        raise ValueError("`min_samples` must be greater than zero")

    if max_trials < 0:
        raise ValueError("`max_trials` must be greater than zero")

    # make sure points is list and not tuple, so it can be modified below
    points = np.array(points)

    for _ in range(max_trials):
        # choose random sample set
        random_idxs = np.random.randint(0, len(points), min_samples)
        samples = points[random_idxs]
        # for d in points:
        #     samples.append(d[random_idxs])

        # estimate model for current random sample set
        model = model_class()
        success = model.estimate(samples)

        if success is not None:  # backwards compatibility
            if not success:
                continue

        model_residuals = np.abs(model.residuals(points))
        # consensus set / inliers
        model_inliers = model_residuals < residual_threshold
        model_fit = model.criterion(points_all, weights, labels, table_prob)

        # choose as new best model if number of inliers is maximal
        sample_inlier_num = np.sum(model_inliers)
        if model_fit < best_model_fit:
            best_model = model
            best_model_fit = model_fit
            if sample_inlier_num > best_inlier_num:
                best_inliers = model_inliers
                best_inlier_num = sample_inlier_num

    # estimate final model using all inliers
    if best_inliers is not None:
        points = points[best_inliers]
        best_model.estimate(points)

    return best_model, best_inliers


def get_slic_points_labels(segm, img=None, slic_size=20, slic_regul=0.1):
    """ run SLIC on image or supepixels and return superpixels, their centers
    and also lebels (label from segmentation in position of superpixel centre)

    :param ndarray segm:
    :param ndarray img:
    :param int slic_size: superpixel size
    :param float slic_regul: regularisation in range (0, 1)
    :return:
    """
    if img is None:
        img = segm / float(segm.max())
    slic = seg_spx.segment_slic_img2d(img, sp_size=slic_size,
                                      rltv_compact=slic_regul)
    slic_centers = np.array(seg_spx.superpixel_centers(slic)).astype(int)
    labels = segm[slic_centers[:, 0], slic_centers[:, 1]]
    return slic, slic_centers, labels


def add_overlap_ellipse(segm, ellipse_params, label, thr_overlap=1.):
    """ add to existing image ellipse with specific label
    if the new ellipse does not ouvelap with already existing object / ellipse

    :param ndarray segm:
    :param () ellipse_params:
    :param int label:
    :param float thr_overlap: relative overlap with existing objects
    :return:

    >>> seg = np.zeros((15, 20), dtype=int)
    >>> ell_params = 7, 10, 5, 8, np.deg2rad(30)
    >>> add_overlap_ellipse(seg, ell_params, 1)
    array([[0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
           [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
           [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
           [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
           [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
           [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
           [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
           [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
           [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0],
           [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0],
           [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]])
    """
    if ellipse_params is None:
        return segm
    mask = np.zeros(segm.shape)
    c1, c2, h, w, phi = ellipse_params
    rr, cc = tl_visu.ellipse(int(c1), int(c2), int(h), int(w), orientation=phi,
                             shape=segm.shape)
    mask[rr, cc] = 1

    # filter overlapping ellipses
    for lb in range(1, int(np.max(segm) + 1)):
        overlap = np.sum(np.logical_and(segm == lb, mask == 1))
        # together = np.sum(np.logical_or(segm == lb, mask == 1))
        # ratio = float(overlap) / float(together)
        sizes = [np.sum(segm == lb), np.sum(mask == 1)]
        ratio = float(overlap) / float(min(sizes))
        # if there is already ellipse with such size, return just the segment
        if ratio > thr_overlap:
            return segm
    segm[mask == 1] = label
    return segm


def prepare_boundary_points_ray_join(seg, centers, close_points=5,
                                     min_diam=MIN_ELLIPSE_DAIM,
                                     sel_bg=STRUC_ELEM_BG,
                                     sel_fg=STRUC_ELEM_FG):
    """ extract some point around foreground boundaries

    :param ndarray seg: input segmentation
    :param [(int, int)] centers: list of centers
    :param float close_points: remove closest point then a given threshold
    :param int min_diam: minimal size of expected objest
    :param int sel_bg: smoothing background with morphological operation
    :param int sel_fg: smoothing foreground with morphological operation
    :return [ndarray]:

    >>> seg = np.zeros((10, 20), dtype=int)
    >>> ell_params = 5, 10, 4, 6, np.deg2rad(30)
    >>> seg = add_overlap_ellipse(seg, ell_params, 1)
    >>> pts = prepare_boundary_points_ray_join(seg, [(4, 9)], 5, 3,
    ...                                        sel_bg=1, sel_fg=0)
    >>> np.round(pts).tolist()  # doctest: +NORMALIZE_WHITESPACE
    [[[4.0, 16.0],
      [7.0, 10.0],
      [9.0, 6.0],
      [1.0, 9.0],
      [4.0, 16.0],
      [7.0, 10.0],
      [1.0, 9.0]]]

    """
    seg_bg, seg_fg = split_segm_background_foreground(seg, sel_bg, sel_fg)

    points_centers = []
    for center in centers:
        ray_bg = seg_fts.compute_ray_features_segm_2d(seg_bg, center)
        ray_bg[ray_bg < min_diam] = min_diam
        points_bg = seg_fts.reconstruct_ray_features_2d(center, ray_bg)
        points_bg = seg_fts.reduce_close_points(points_bg, close_points)

        ray_fc = seg_fts.compute_ray_features_segm_2d(seg_fg, center,
                                                      edge='down')
        ray_fc[ray_fc < min_diam] = min_diam
        points_fc = seg_fts.reconstruct_ray_features_2d(center, ray_fc)
        points_fc = seg_fts.reduce_close_points(points_fc, close_points)

        points_both = np.vstack((points_bg, points_fc))
        points_centers.append(points_both)
    return points_centers


def split_segm_background_foreground(seg, sel_bg=STRUC_ELEM_BG,
                                     sel_fg=STRUC_ELEM_FG):
    """ smoothing segmentation with morphological operation

    :param ndarray seg: input segmentation
    :param int sel_bg: smoothing background with morphological operation
    :param int sel_fg: smoothing foreground with morphological operation
    :return:

    >>> seg = np.zeros((10, 20), dtype=int)
    >>> ell_params = 5, 10, 4, 6, np.deg2rad(30)
    >>> seg = add_overlap_ellipse(seg, ell_params, 1)
    >>> seg_bg, seg_fc = split_segm_background_foreground(seg, 1.5, 0)
    >>> seg_bg.astype(int)
    array([[1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
           [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
           [1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1],
           [1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1],
           [1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1],
           [1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1],
           [1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
           [1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
           [1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1],
           [1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1]])
    >>> seg_fc.astype(int)
    array([[0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0],
           [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]])
    """
    seg_bg = (seg > 0)
    seg_bg = 1 - ndimage.morphology.binary_fill_holes(seg_bg)
    if sel_bg > 0:
        seg_bg = morphology.opening(seg_bg, morphology.disk(sel_bg))

    seg_fg = (seg == 1)
    if sel_fg > 0:
        seg_fg = morphology.opening(seg_fg, morphology.disk(sel_fg))
    return seg_bg, seg_fg


def prepare_boundary_points_ray_edge(seg, centers, close_points=5,
                                     min_diam=MIN_ELLIPSE_DAIM,
                                     sel_bg=STRUC_ELEM_BG,
                                     sel_fg=STRUC_ELEM_FG):
    """ extract some point around foreground boundaries

    :param ndarray seg: input segmentation
    :param [(int, int)] centers: list of centers
    :param float close_points: remove closest point then a given threshold
    :param int min_diam: minimal size of expected objest
    :param int sel_bg: smoothing background with morphological operation
    :param int sel_fg: smoothing foreground with morphological operation
    :return [ndarray]:

    >>> seg = np.zeros((10, 20), dtype=int)
    >>> ell_params = 5, 10, 4, 6, np.deg2rad(30)
    >>> seg = add_overlap_ellipse(seg, ell_params, 1)
    >>> pts = prepare_boundary_points_ray_edge(seg, [(4, 9)], 5, 3,
    ...                                        sel_bg=1, sel_fg=0)
    >>> np.round(pts).tolist()  # doctest: +NORMALIZE_WHITESPACE
    [[[4.0, 16.0],
      [9.0, 6.0],
      [1.0, 9.0]]]
    """
    seg_bg, seg_fc = split_segm_background_foreground(seg, sel_bg, sel_fg)

    points_centers = []
    for center in centers:
        ray_bg = seg_fts.compute_ray_features_segm_2d(seg_bg, center)

        ray_fc = seg_fts.compute_ray_features_segm_2d(seg_fc, center,
                                                      edge='down')

        # replace not found (-1) by large values
        rays = np.array([ray_bg, ray_fc], dtype=float)
        rays[rays < 0] = np.inf
        rays[rays < min_diam] = min_diam
        # take the smallesr from both
        ray_close = np.min(rays, axis=0)
        points_close = seg_fts.reconstruct_ray_features_2d(center, ray_close)
        points_close = seg_fts.reduce_close_points(points_close, close_points)

        points_centers.append(points_close)
    return points_centers


def prepare_boundary_points_ray_mean(seg, centers, close_points=5,
                                     min_diam=MIN_ELLIPSE_DAIM,
                                     sel_bg=STRUC_ELEM_BG,
                                     sel_fg=STRUC_ELEM_FG):
    """ extract some point around foreground boundaries

    :param ndarray seg: input segmentation
    :param [(int, int)] centers: list of centers
    :param float close_points: remove closest point then a given threshold
    :param int min_diam: minimal size of expected objest
    :param int sel_bg: smoothing background with morphological operation
    :param int sel_fg: smoothing foreground with morphological operation
    :return [ndarray]:

    >>> seg = np.zeros((10, 20), dtype=int)
    >>> ell_params = 5, 10, 4, 6, np.deg2rad(30)
    >>> seg = add_overlap_ellipse(seg, ell_params, 1)
    >>> pts = prepare_boundary_points_ray_mean(seg, [(4, 9)], 5, 3,
    ...                                        sel_bg=1, sel_fg=0)
    >>> np.round(pts).tolist()  # doctest: +NORMALIZE_WHITESPACE
    [[[4.0, 16.0],
      [9.0, 6.0],
      [1.0, 9.0]]]
    """
    seg_bg, seg_fc = split_segm_background_foreground(seg, sel_bg, sel_fg)

    points_centers = []
    for center in centers:
        ray_bg = seg_fts.compute_ray_features_segm_2d(seg_bg, center)

        ray_fc = seg_fts.compute_ray_features_segm_2d(seg_fc, center,
                                                      edge='down')

        # replace not found (-1) by large values
        rays = np.array([ray_bg, ray_fc], dtype=float)
        rays[rays < 0] = np.inf
        rays[rays < min_diam] = min_diam

        # take the smalles from both
        ray_min = np.min(rays, axis=0)
        ray_mean = np.mean(rays, axis=0)
        ray_mean[np.isinf(ray_mean)] = ray_min[np.isinf(ray_mean)]

        points_close = seg_fts.reconstruct_ray_features_2d(center, ray_mean)
        points_close = seg_fts.reduce_close_points(points_close, close_points)

        points_centers.append(points_close)
    return points_centers


def prepare_boundary_points_ray_dist(seg, centers, close_points=1,
                                     sel_bg=STRUC_ELEM_BG,
                                     sel_fg=STRUC_ELEM_FG):
    """ extract some point around foreground boundaries

    :param ndarray seg: input segmentation
    :param [(int, int)] centers: list of centers
    :param float close_points: remove closest point then a given threshold
    :param int sel_bg: smoothing background with morphological operation
    :param int sel_fg: smoothing foreground with morphological operation
    :return [ndarray]:

    >>> seg = np.zeros((10, 20), dtype=int)
    >>> ell_params = 5, 10, 4, 6, np.deg2rad(30)
    >>> seg = add_overlap_ellipse(seg, ell_params, 1)
    >>> pts = prepare_boundary_points_ray_dist(seg, [(4, 9)], 2,
    ...                                        sel_bg=0, sel_fg=0)
    >>> np.round(pts).tolist()  # doctest: +NORMALIZE_WHITESPACE
    [[[0.0, 2.0],
      [4.0, 16.0],
      [6.0, 15.0],
      [9.0, 6.0],
      [6.0, 5.0],
      [3.0, 7.0],
      [0.0, 10.0]]]
    """
    seg_bg, _ = split_segm_background_foreground(seg, sel_bg, sel_fg)

    points = np.array((0, np.asarray(centers).shape[1]))
    for center in centers:
        ray = seg_fts.compute_ray_features_segm_2d(seg_bg, center)
        points_bg = seg_fts.reconstruct_ray_features_2d(center, ray, 0)
        points_bg = seg_fts.reduce_close_points(points_bg, close_points)

        points = np.vstack((points, points_bg))

    dists = spatial.distance.cdist(points, centers, metric='euclidean')
    close_center = np.argmin(dists, axis=1)

    points_centers = []
    for i in range(close_center.max() + 1):
        pts = points[close_center == i]
        points_centers.append(pts)
    return points_centers


def filter_boundary_points(segm, slic):
    slic_centers = np.array(seg_spx.superpixel_centers(slic)).astype(int)
    labels = segm[slic_centers[:, 0], slic_centers[:, 1]]

    vertices, edges = seg_spx.make_graph_segm_connect2d_conn4(slic)
    nb_labels = labels.max() + 1

    neighbour_labels = np.zeros((len(vertices), nb_labels))
    for e1, e2 in edges:
        # print e1, labels[e2], e2, labels[e1]
        neighbour_labels[e1, labels[e2]] += 1
        neighbour_labels[e2, labels[e1]] += 1
    neighbour_labels = neighbour_labels \
                       / np.tile(np.sum(neighbour_labels, axis=1),
                                 (nb_labels, 1)).T

    # border point nex to foreground
    filter_bg = np.logical_and(labels == 0, neighbour_labels[:, 0] < 1)
    # fulucul cels next to backround
    filter_fc = np.logical_and(labels == 1, neighbour_labels[:, 0] > 0)
    points = slic_centers[np.logical_or(filter_bg, filter_fc)]

    return points


def prepare_boundary_points_close(seg, centers, sp_size=25, rltv_compact=0.3):
    """ extract some point around foreground boundaries

    :param ndarray seg: input segmentation
    :param [(int, int)] centers: list of centers
    :return [ndarray]:

    >>> seg = np.zeros((100, 200), dtype=int)
    >>> ell_params = 50, 100, 40, 60, np.deg2rad(30)
    >>> seg = add_overlap_ellipse(seg, ell_params, 1)
    >>> pts = prepare_boundary_points_close(seg, [(40, 90)])
    >>> sorted(np.round(pts).tolist())  # doctest: +NORMALIZE_WHITESPACE
    [[[6, 85], [8, 150], [16, 109], [27, 139], [32, 77], [36, 41], [34, 177],
    [59, 161], [54, 135], [67, 62], [64, 33], [84, 150], [91, 48], [92, 118]]]
    """
    slic = seg_spx.segment_slic_img2d(seg / float(seg.max()), sp_size=sp_size,
                                      rltv_compact=rltv_compact)
    points_all = filter_boundary_points(seg, slic)

    dists = spatial.distance.cdist(points_all, centers, metric='euclidean')
    close_center = np.argmin(dists, axis=1)

    points_centers = []
    for i in range(int(close_center.max() + 1)):
        points = points_all[close_center == i]
        points_centers.append(points)
    return points_centers


# def find_dist_hist_local_minim(dists, nb_bins=25, gauss_sigma=1):
#     hist, bin = np.histogram(dists, bins=nb_bins)
#     hist = ndimage.filters.gaussian_filter1d(hist, sigma=gauss_sigma)
#     # bins = (bin[1:] + bin[:-1]) / 2.
#     # idxs = peakutils.indexes(-hist, thres=0, min_dist=1)
#     coord = feature.peak_local_max(-hist, min_distance=4).tolist() + [
#         [len(hist) - 1]]
#     thr_dist = bin[coord[0][0]]
#     return thr_dist


# def prepare_boundary_points_dist(seg, centers, sp_size=25, rltv_compact=0.3):
#     """ extract some point around foreground boundaries
#
#     :param ndarray seg: input segmentation
#     :param [(int, int)] centers: list of centers
#     :return [ndarray]:
#
#     >>> seg = np.zeros((100, 200), dtype=int)
#     >>> ell_params = 50, 100, 40, 60, 30
#     >>> seg = add_overlap_ellipse(seg, ell_params, 1)
#     >>> pts = prepare_boundary_points_dist(seg, [(40, 90)])
#     >>> sorted(np.round(pts).tolist())  # doctest: +NORMALIZE_WHITESPACE
#     [[[8, 63], [5, 79], [6, 97], [7, 117], [19, 73], [19, 85], [19, 95],
#      [19, 107], [21, 119], [24, 62], [28, 129], [33, 51], [46, 47],
#      [60, 50], [70, 60], [74, 71], [80, 81], [83, 93]]]
#     """
#     slic = seg_spx.segment_slic_img2d(seg / float(seg.max()), sp_size=sp_size,
#                                      rltv_compact=rltv_compact)
#     points_all = filter_boundary_points(seg, slic)
#
#     dists = spatial.distance.cdist(points_all, centers, metric='euclidean')
#     close_center = np.argmin(dists, axis=1)
#     dist_min = np.min(dists, axis=1)
#
#     points_centers = []
#     for i in range(int(close_center.max() + 1)):
#         dist_thr = find_dist_hist_local_minim(dist_min[close_center == i])
#         points = points_all[np.logical_and(close_center == i,
#                                        dist_min <= dist_thr)]
#         points_centers.append(points)
#     return points_centers