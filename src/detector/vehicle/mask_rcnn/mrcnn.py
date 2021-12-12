import numpy as np
import tensorflow.keras.layers as KL
import tensorflow.keras.models as KM

from .graphs import (
    build_fpn_mask_graph,
    build_rpn_model,
    fpn_classifier_graph,
    resnet_graph,
    )
from .layers import (
    DetectionLayer,
    ProposalLayer,
    )
from .utils import (
    compose_image_meta,
    compute_backbone_shapes,
    denorm_boxes,
    generate_pyramid_anchors,
    mold_image,
    norm_boxes,
    resize_image,
    unmold_mask,
    )


class MaskRCNN:
    """Encapsulates the Mask RCNN model functionality.

    The actual Keras model is in the keras_model property.
    """

    def __init__(self, config):
        """
        mode: Either "training" or "inference"
        config: A Sub-class of the Config class
        """
        self.config = config
        self.keras_model = self.build(config)

    @staticmethod
    def build(config):
        """Build Mask R-CNN architecture.
            input_shape: The shape of the input image.
            mode: Either "training" or "inference". The inputs and
                outputs of the model differ accordingly.
        """
        # Image size must be dividable by 2 multiple times
        h, w = config.IMAGE_SHAPE[:2]
        if h / 2 ** 6 != int(h / 2 ** 6) or w / 2 ** 6 != int(w / 2 ** 6):
            raise Exception(
                    "Image size must be dividable by 2 at least 6 times "
                    "to avoid fractions when downscaling and upscaling."
                    "For example, use 256, 320, 384, 448, 512, ... etc. "
                    )

        # Inputs
        input_image = KL.Input(
                shape=[None, None, config.IMAGE_SHAPE[2]], name="input_image"
                )
        input_image_meta = KL.Input(
                shape=[config.IMAGE_META_SIZE],
                name="input_image_meta"
                )
        input_anchors = KL.Input(shape=[None, 4], name="input_anchors")

        # Build the shared convolutional layers.
        # Bottom-up Layers
        # Returns a list of the last layers of each stage, 5 in total.
        # Don't create the thead (stage 5), so we pick the 4th item in the list.
        if callable(config.BACKBONE):
            _, C2, C3, C4, C5 = config.BACKBONE(
                    input_image, stage5=True,
                    train_bn=config.TRAIN_BN
                    )
        else:
            _, C2, C3, C4, C5 = resnet_graph(
                    input_image, config.BACKBONE,
                    stage5=True, train_bn=config.TRAIN_BN
                    )
        # Top-down Layers
        # TODO: add assert to varify feature map sizes match what's in config
        P5 = KL.Conv2D(config.TOP_DOWN_PYRAMID_SIZE, (1, 1), name='fpn_c5p5')(C5)
        P4 = KL.Add(name="fpn_p4add")(
                [
                    KL.UpSampling2D(size=(2, 2), name="fpn_p5upsampled")(P5),
                    KL.Conv2D(config.TOP_DOWN_PYRAMID_SIZE, (1, 1), name='fpn_c4p4')(C4)]
                )
        P3 = KL.Add(name="fpn_p3add")(
                [
                    KL.UpSampling2D(size=(2, 2), name="fpn_p4upsampled")(P4),
                    KL.Conv2D(config.TOP_DOWN_PYRAMID_SIZE, (1, 1), name='fpn_c3p3')(C3)]
                )
        P2 = KL.Add(name="fpn_p2add")(
                [
                    KL.UpSampling2D(size=(2, 2), name="fpn_p3upsampled")(P3),
                    KL.Conv2D(config.TOP_DOWN_PYRAMID_SIZE, (1, 1), name='fpn_c2p2')(C2)]
                )
        # Attach 3x3 conv to all P layers to get the final feature maps.
        P2 = KL.Conv2D(config.TOP_DOWN_PYRAMID_SIZE, (3, 3), padding="SAME", name="fpn_p2")(P2)
        P3 = KL.Conv2D(config.TOP_DOWN_PYRAMID_SIZE, (3, 3), padding="SAME", name="fpn_p3")(P3)
        P4 = KL.Conv2D(config.TOP_DOWN_PYRAMID_SIZE, (3, 3), padding="SAME", name="fpn_p4")(P4)
        P5 = KL.Conv2D(config.TOP_DOWN_PYRAMID_SIZE, (3, 3), padding="SAME", name="fpn_p5")(P5)
        # P6 is used for the 5th anchor scale in RPN. Generated by
        # subsampling from P5 with stride of 2.
        P6 = KL.MaxPooling2D(pool_size=(1, 1), strides=2, name="fpn_p6")(P5)

        # Note that P6 is used in RPN, but not in the classifier heads.
        rpn_feature_maps = [P2, P3, P4, P5, P6]
        mrcnn_feature_maps = [P2, P3, P4, P5]

        anchors = input_anchors

        # RPN Model
        rpn = build_rpn_model(
                config.RPN_ANCHOR_STRIDE,
                len(config.RPN_ANCHOR_RATIOS), config.TOP_DOWN_PYRAMID_SIZE
                )
        # Loop through pyramid layers
        layer_outputs = []  # list of lists
        for p in rpn_feature_maps:
            layer_outputs.append(rpn([p]))
        # Concatenate layer outputs
        # Convert from list of lists of level outputs to list of lists
        # of outputs across levels.
        # e.g. [[a1, b1, c1], [a2, b2, c2]] => [[a1, a2], [b1, b2], [c1, c2]]
        output_names = ["rpn_class_logits", "rpn_class", "rpn_bbox"]
        outputs = list(zip(*layer_outputs))
        outputs = [KL.Concatenate(axis=1, name=n)(list(o))
                   for o, n in zip(outputs, output_names)]

        rpn_class_logits, rpn_class, rpn_bbox = outputs

        # Generate proposals
        # Proposals are [batch, N, (y1, x1, y2, x2)] in normalized coordinates
        # and zero padded.
        proposal_count = config.POST_NMS_ROIS_INFERENCE
        rpn_rois = ProposalLayer(
                proposal_count=proposal_count,
                nms_threshold=config.RPN_NMS_THRESHOLD,
                name="ROI",
                config=config
                )([rpn_class, rpn_bbox, anchors])

        # Network Heads
        # Proposal classifier and BBox regressor heads
        mrcnn_class_logits, mrcnn_class, mrcnn_bbox = \
            fpn_classifier_graph(
                    rpn_rois, mrcnn_feature_maps, input_image_meta,
                    config.POOL_SIZE, config.NUM_CLASSES,
                    train_bn=config.TRAIN_BN,
                    fc_layers_size=config.FPN_CLASSIF_FC_LAYERS_SIZE
                    )

        # Detections
        # output is [batch, num_detections, (y1, x1, y2, x2, class_id, score)] in
        # normalized coordinates
        detections = DetectionLayer(config, name="mrcnn_detection")(
                [rpn_rois, mrcnn_class, mrcnn_bbox, input_image_meta]
                )

        # Create masks for detections
        detection_boxes = KL.Lambda(lambda x: x[..., :4])(detections)
        mrcnn_mask = build_fpn_mask_graph(
                detection_boxes, mrcnn_feature_maps,
                input_image_meta,
                config.MASK_POOL_SIZE,
                config.NUM_CLASSES,
                train_bn=config.TRAIN_BN
                )

        model = KM.Model(
                [input_image, input_image_meta, input_anchors],
                [detections, mrcnn_class, mrcnn_bbox,
                 mrcnn_mask, rpn_rois, rpn_class, rpn_bbox],
                name='mask_rcnn'
                )

        return model

    def load_weights(self, filepath, by_name=False, exclude=None):
        """Modified version of the corresponding Keras function with
        the addition of multi-GPU support and the ability to exclude
        some layers from loading.
        exclude: list of layer names to exclude
        """
        self.keras_model.load_weights(filepath=filepath, by_name=by_name)

    def mold_inputs(self, images):
        """Takes a list of images and modifies them to the format expected
        as an input to the neural network.
        images: List of image matrices [height,width,depth]. Images can have
            different sizes.

        Returns 3 Numpy matrices:
        molded_images: [N, h, w, 3]. Images resized and normalized.
        image_metas: [N, length of meta data]. Details about each image.
        windows: [N, (y1, x1, y2, x2)]. The portion of the image that has the
            original image (padding excluded).
        """
        molded_images = []
        image_metas = []
        windows = []
        for image in images:
            # Resize image
            molded_image, window, scale, padding, crop = resize_image(
                    image,
                    min_dim=self.config.IMAGE_MIN_DIM,
                    min_scale=self.config.IMAGE_MIN_SCALE,
                    max_dim=self.config.IMAGE_MAX_DIM,
                    mode=self.config.IMAGE_RESIZE_MODE
                    )
            molded_image = mold_image(molded_image, self.config)
            # Build image_meta
            image_meta = compose_image_meta(
                    0, image.shape, molded_image.shape, window, scale,
                    np.zeros([self.config.NUM_CLASSES], dtype=np.int32)
                    )
            # Append
            molded_images.append(molded_image)
            windows.append(window)
            image_metas.append(image_meta)
        # Pack into arrays
        molded_images = np.stack(molded_images)
        image_metas = np.stack(image_metas)
        windows = np.stack(windows)
        return molded_images, image_metas, windows

    @staticmethod
    def unmold_detections(detections, mrcnn_mask, original_image_shape,
                          image_shape, window
                          ):
        """Reformats the detections of one image from the format of the neural
        network output to a format suitable for use in the rest of the
        application.

        detections: [N, (y1, x1, y2, x2, class_id, score)] in normalized coordinates
        mrcnn_mask: [N, height, width, num_classes]
        original_image_shape: [H, W, C] Original image shape before resizing
        image_shape: [H, W, C] Shape of the image after resizing and padding
        window: [y1, x1, y2, x2] Pixel coordinates of box in the image where the real
                image is excluding the padding.

        Returns:
        boxes: [N, (y1, x1, y2, x2)] Bounding boxes in pixels
        class_ids: [N] Integer class IDs for each bounding box
        scores: [N] Float probability scores of the class_id
        masks: [height, width, num_instances] Instance masks
        """
        # How many detections do we have?
        # Detections array is padded with zeros. Find the first class_id == 0.
        zero_ix = np.where(detections[:, 4] == 0)[0]
        N = zero_ix[0] if zero_ix.shape[0] > 0 else detections.shape[0]

        # Extract boxes, class_ids, scores, and class-specific masks
        boxes = detections[:N, :4]
        class_ids = detections[:N, 4].astype(np.int32)
        scores = detections[:N, 5]
        masks = mrcnn_mask[np.arange(N), :, :, class_ids]

        # Translate normalized coordinates in the resized image to pixel
        # coordinates in the original image before resizing
        window = norm_boxes(window, image_shape[:2])
        wy1, wx1, wy2, wx2 = window
        shift = np.array([wy1, wx1, wy1, wx1])
        wh = wy2 - wy1  # window height
        ww = wx2 - wx1  # window width
        scale = np.array([wh, ww, wh, ww])
        # Convert boxes to normalized coordinates on the window
        boxes = np.divide(boxes - shift, scale)
        # Convert boxes to pixel coordinates on the original image
        boxes = denorm_boxes(boxes, original_image_shape[:2])

        # Filter out detections with zero area. Happens in early training when
        # network weights are still random
        exclude_ix = np.where(
                (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1]) <= 0
                )[0]
        if exclude_ix.shape[0] > 0:
            boxes = np.delete(boxes, exclude_ix, axis=0)
            class_ids = np.delete(class_ids, exclude_ix, axis=0)
            scores = np.delete(scores, exclude_ix, axis=0)
            masks = np.delete(masks, exclude_ix, axis=0)
            N = class_ids.shape[0]

        # Resize masks to original image size and set boundary threshold.
        full_masks = []
        for i in range(N):
            # Convert neural network mask to full size mask
            full_mask = unmold_mask(masks[i], boxes[i], original_image_shape)
            full_masks.append(full_mask)
        full_masks = np.stack(full_masks, axis=-1) \
            if full_masks else np.empty(original_image_shape[:2] + (0,))

        return boxes, class_ids, scores, full_masks

    def detect(self, images, verbose=0):
        """Runs the detection pipeline.

        images: List of images, potentially of different sizes.

        Returns a list of dicts, one dict per image. The dict contains:
        rois: [N, (y1, x1, y2, x2)] detection bounding boxes
        class_ids: [N] int class IDs
        scores: [N] float probability scores for the class IDs
        masks: [H, W, N] instance binary masks
        """
        assert len(
                images
                ) == self.config.BATCH_SIZE, "len(images) must be equal to BATCH_SIZE"

        # Mold inputs to format expected by the neural network
        molded_images, image_metas, windows = self.mold_inputs(images)

        # Validate image sizes
        # All images in a batch MUST be of the same size
        image_shape = molded_images[0].shape
        for g in molded_images[1:]:
            assert g.shape == image_shape, \
                "After resizing, all images must have the same size. Check IMAGE_RESIZE_MODE and image sizes."

        # Anchors
        anchors = self.get_anchors(image_shape)
        # Duplicate across the batch dimension because Keras requires it
        # TODO: can this be optimized to avoid duplicating the anchors?
        anchors = np.broadcast_to(anchors, (self.config.BATCH_SIZE,) + anchors.shape)

        # Run object detection
        detections, _, _, mrcnn_mask, _, _, _ = \
            self.keras_model.predict([molded_images, image_metas, anchors])
        # Process detections
        results = []
        for i, image in enumerate(images):
            final_rois, final_class_ids, final_scores, final_masks = \
                self.unmold_detections(
                        detections[i], mrcnn_mask[i],
                        image.shape, molded_images[i].shape,
                        windows[i]
                        )
            results.append(
                    {
                        "rois": final_rois,
                        "class_ids": final_class_ids,
                        "scores": final_scores,
                        "masks": final_masks,
                        }
                    )
        return results

    def get_anchors(self, image_shape):
        """Returns anchor pyramid for the given image size."""
        backbone_shapes = compute_backbone_shapes(self.config, image_shape)
        # Cache anchors and reuse if image shape is the same
        if not hasattr(self, "_anchor_cache"):
            self._anchor_cache = {}
        if not tuple(image_shape) in self._anchor_cache:
            # Generate Anchors
            a = generate_pyramid_anchors(
                    self.config.RPN_ANCHOR_SCALES,
                    self.config.RPN_ANCHOR_RATIOS,
                    backbone_shapes,
                    self.config.BACKBONE_STRIDES,
                    self.config.RPN_ANCHOR_STRIDE
                    )
            # Keep a copy of the latest anchors in pixel coordinates because
            # it's used in inspect_model notebooks.
            self.anchors = a
            # Normalize coordinates
            self._anchor_cache[tuple(image_shape)] = norm_boxes(a, image_shape[:2])
        return self._anchor_cache[tuple(image_shape)]
