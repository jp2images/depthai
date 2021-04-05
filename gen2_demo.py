#!/usr/bin/env python3
import argparse
import json
import time
from pathlib import Path
import sys
from types import SimpleNamespace

import cv2
import depthai as dai
import numpy as np

from depthai_helpers.arg_manager import CNN_choices
from depthai_helpers.config_manager import BlobManager
from gen2_helpers import frame_norm, to_planar

parser = argparse.ArgumentParser()
parser.add_argument('-nd', '--no-debug', action="store_true", help="Prevent debug output")
parser.add_argument('-cam', '--camera', action="store_true",
                    help="Use DepthAI 4K RGB camera for inference (conflicts with -vid)")
parser.add_argument('-vid', '--video', type=str,
                    help="Path to video file to be used for inference (conflicts with -cam)")
parser.add_argument('-lq', '--lowquality', action="store_true", help="Low quality visualization - uses resized frames")
parser.add_argument('-d', '--depth', action="store_true", help="Use depth information")
parser.add_argument('-cnnp', '--cnn_path', type=Path, help="Path to cnn model directory to be run")
parser.add_argument("-cnn", "--cnn_model", default="mobilenet-ssd", type=str, choices=CNN_choices, help="Cnn model to run on DepthAI")
parser.add_argument('-sh', '--shaves', default=13, type=int, help="Name of the nn to be run from default depthai repository")
parser.add_argument('-cnn-size', '--cnn-input-size', default=None, help="Neural network input dimensions, in \"WxH\" format, e.g. \"544x320\"")
parser.add_argument("-rgbf", "--rgb_fps", default=30.0, type=float, help="RGB cam fps: max 118.0 for H:1080, max 42.0 for H:2160. Default: %(default)s")

def dct_range(arg):
    try:
        val = int(arg)
    except ValueError:
        raise argparse.ArgumentTypeError("Must be an integer number")
    if 255 < val or val < 0:
        raise argparse.ArgumentTypeError("Argument must be between 0 and 255")
    return val
parser.add_argument("-dct", "--disparity_confidence_threshold", default=200, type=dct_range, help="Disparity confidence threshold, used for depth measurement. Default: %(default)s")
parser.add_argument("-med", "--stereo_median_size", default=7, type=int, choices=[0,3,5,7], help="Disparity / depth median filter kernel size (N x N) . 0 = filtering disabled. Default: %(default)s")
parser.add_argument('-lrc', '--stereo_lr_check', action="store_true", help="Enable stereo 'Left-Right check' feature.")
args = parser.parse_args()

debug = not args.no_debug
camera = not args.video
hq = not args.lowquality
depth = args.depth

default_input_dims = {
    # TODO remove once fetching input size from nn blob is possible
    "mobilenet-ssd": "300x300",
    "face-detection-adas-0001": "672x384",
    "face-detection-retail-0004": "300x300",
    "pedestrian-detection-adas-0002": "672x384",
    "person-detection-retail-0013": "544x320",
    "person-vehicle-bike-detection-crossroad-1016": "512x512",
    "vehicle-detection-adas-0002": "672x384",
    "vehicle-license-plate-detection-barrier-0106": "300x300",
    "tiny-yolo-v3": "416x416",
    "yolo-v3": "416x416"
}

if args.cnn_input_size is None:
    if args.cnn_model not in default_input_dims:
        raise RuntimeError("Unable to determine the nn input size. Please use -nn-size flag to specify it in WxW format: -nn-size <width>x<height>")
    in_w, in_h = map(int, default_input_dims[args.cnn_model].split('x'))
else:
    in_w, in_h = map(int, args.cnn_input_size.split('x'))

if args.stereo_median_size == 0: median = dai.StereoDepthProperties.MedianFilter.MEDIAN_OFF
elif args.stereo_median_size == 3: median = dai.StereoDepthProperties.MedianFilter.KERNEL_3x3
elif args.stereo_median_size == 5: median = dai.StereoDepthProperties.MedianFilter.KERNEL_5x5
elif args.stereo_median_size == 7: median = dai.StereoDepthProperties.MedianFilter.KERNEL_7x7
class NNetManager:
    source_choices = ("rgb", "left", "right", "host")
    config = None
    nn_family = None
    confidence = None
    metadata = None
    output_format = None
    device = None
    input = None
    output = None

    def __init__(self, model_dir, source, use_depth, use_hq):
        if source not in self.source_choices:
            raise RuntimeError(f"Source {source} is invalid, available {self.source_choices}")
        self.source = source
        self.model_dir = model_dir
        self.use_depth = use_depth
        self.use_hq = use_hq
        self.model_name = self.model_dir.name
        self.output_name = f"{self.model_name}_out"
        self.input_name = f"{self.model_name}_in"
        self.blob_path = BlobManager(model_dir=self.model_dir.resolve().absolute()).compile(args.shaves)

        cofnig_path = self.model_dir / Path(self.model_name).with_suffix(f".json")
        if cofnig_path.exists():
            with cofnig_path.open() as f:
                self.config = json.load(f)
                nn_config = self.config.get("NN_config", {})
                self.labels = self.config.get("mappings", {}).get("labels", None)
                self.nn_family = nn_config.get("NN_family", None)
                self.output_format = nn_config.get("output_format", None)
                self.metadata = nn_config.get("NN_specific_metadata", {})

                self.confidence = self.metadata.get("confidence_threshold", nn_config.get("confidence_threshold", None))

    def addDevice(self, device):
        self.device = device
        self.input = device.getInputQueue(self.input_name, maxSize=1, blocking=False) if self.source == "host" else None
        self.output = device.getOutputQueue(self.output_name, maxSize=1, blocking=False)
        self.bb_mapping = device.getOutputQueue("bb_mapping", maxSize=1, blocking=False) if self.use_depth else None

    def create_nn_pipeline(self, p, nodes):
        if self.nn_family == "mobilenet":
            nn = p.createMobileNetSpatialDetectionNetwork() if self.use_depth else p.createMobileNetDetectionNetwork()
        elif self.nn_family == "YOLO":
            nn = p.createYoloSpatialDetectionNetwork() if self.use_depth else p.createYoloDetectionNetwork()
            nn.setConfidenceThreshold(0.5)
            nn.setNumClasses(self.metadata["classes"])
            nn.setCoordinateSize(self.metadata["coordinates"])
            nn.setAnchors(self.metadata["anchors"])
            nn.setAnchorMasks(self.metadata["anchor_masks"])
            nn.setIouThreshold(self.metadata["iou_threshold"])
        else:
            # TODO use createSpatialLocationCalculator
            nn = p.createNeuralNetwork()

        # If we use depth information, we also send bounding box mapping to the host
        if self.use_depth and (self.nn_family == "YOLO" or self.nn_family == "mobilenet"):
            nodes.xout_bb_mapping = p.createXLinkOut()
            nodes.xout_bb_mapping.setStreamName("bb_mapping")
            nn.boundingBoxMapping.link(nodes.xout_bb_mapping.input)

        nn.setBlobPath(str(self.blob_path))
        nn.setConfidenceThreshold(self.confidence)
        nn.setNumInferenceThreads(2)
        nn.input.setBlocking(False)
        nn.input.setQueueSize(2)
        xout = p.createXLinkOut()
        xout.setStreamName(self.output_name)
        nn.out.link(xout.input)
        setattr(nodes, self.model_name, nn)
        setattr(nodes, self.output_name, xout)
        if self.source == "rgb":
            nodes.cam_rgb.preview.link(nn.input)
        elif self.source == "host":
            xin = p.createXLinkIn()
            xin.setStreamName(self.input_name)
            xin.out.link(nn.input)
            setattr(nodes, self.input_name, xout)
        elif self.source == "right": # Use spatial information
            nodes.xout_right = p.createXLinkOut()
            nodes.xout_right.setStreamName("right")

            # if self.use_hq:
            #     nodes.mono_right.out.link(nodes.xout_right.input)
            # else:
            nn.passthrough.link(nodes.xout_right.input)

            nodes.manip = p.createImageManip()
            nodes.manip.initialConfig.setResize(in_w, in_h)
            # The NN model expects BGR input. By default ImageManip output type would be same as input (gray in this case)
            nodes.manip.initialConfig.setFrameType(dai.RawImgFrame.Type.BGR888p)
            nodes.manip.out.link(nn.input)

            nodes.stereo.rectifiedRight.link(nodes.manip.inputImage)
            nodes.stereo.depth.link(nn.inputDepth)
            # Spatial configs
            nn.setBoundingBoxScaleFactor(0.3)
            nn.setDepthLowerThreshold(100)
            nn.setDepthUpperThreshold(3000)

    def get_label_text(self, label):
        if self.config is None or self.labels is None:
            return label
        elif int(label) < len(self.labels):
            return self.labels[int(label)]
        else:
            print(f"Label of ouf bounds (label_index: {label}, available_labels: {len(self.labels)}")
            return label


class FPSHandler:
    def __init__(self, cap=None):
        self.timestamp = time.time()
        self.start = time.time()
        self.framerate = cap.get(cv2.CAP_PROP_FPS) if cap is not None else None

        self.frame_cnt = 0
        self.ticks = {}
        self.ticks_cnt = {}

    def next_iter(self):
        if not camera:
            frame_delay = 1.0 / self.framerate
            delay = (self.timestamp + frame_delay) - time.time()
            if delay > 0:
                time.sleep(delay)
        self.timestamp = time.time()
        self.frame_cnt += 1

    def tick(self, name):
        if name in self.ticks:
            self.ticks_cnt[name] += 1
        else:
            self.ticks[name] = time.time()
            self.ticks_cnt[name] = 0

    def tick_fps(self, name):
        if name in self.ticks:
            time_diff = time.time() - self.ticks[name]
            return self.ticks_cnt[name] / time_diff if time_diff != 0 else 0
        else:
            return 0

    def fps(self):
        return self.frame_cnt / (self.timestamp - self.start)


def create_pipeline(use_camera, use_hq, use_depth, nn_pipeline=None):
    # Start defining a pipeline
    p = dai.Pipeline()
    nodes = SimpleNamespace()

    if use_depth:
        nodes.stereo = p.createStereoDepth()
        nodes.stereo.setOutputDepth(True)
        nodes.stereo.setConfidenceThreshold(args.disparity_confidence_threshold)
        nodes.stereo.setOutputRectified(True)
        nodes.stereo.setMedianFilter(median)
        nodes.stereo.setLeftRightCheck(args.stereo_lr_check)

        nodes.mono_left = p.createMonoCamera()
        nodes.mono_left.setResolution(dai.MonoCameraProperties.SensorResolution.THE_400_P)
        nodes.mono_left.setBoardSocket(dai.CameraBoardSocket.LEFT)
        nodes.mono_left.out.link(nodes.stereo.left)

        nodes.mono_right = p.createMonoCamera()
        nodes.mono_right.setResolution(dai.MonoCameraProperties.SensorResolution.THE_400_P)
        nodes.mono_right.setBoardSocket(dai.CameraBoardSocket.RIGHT)
        nodes.mono_right.out.link(nodes.stereo.right)
    elif use_camera:
        # Define a source - color camera
        nodes.cam_rgb = p.createColorCamera()
        nodes.cam_rgb.setPreviewSize(in_w, in_h)
        nodes.cam_rgb.setInterleaved(False)
        nodes.cam_rgb.setFps(args.rgb_fps)
        xout_rgb = p.createXLinkOut()
        xout_rgb.setStreamName("rgb")
        if use_hq:
            nodes.cam_rgb.video.link(xout_rgb.input)
        else:
            nodes.cam_rgb.preview.link(xout_rgb.input)

    if callable(nn_pipeline):
        nn_pipeline(p, nodes)

    return p


nn_manager = NNetManager(
    model_dir=args.cnn_path or Path(__file__).parent / Path(f"resources/nn/{args.cnn_model}/"),
    source="right" if depth else "rgb" if camera else "host",
    use_depth=depth,
    use_hq=hq
)

p = create_pipeline(
    use_camera=camera,
    use_hq=hq,
    use_depth=depth,
    nn_pipeline=nn_manager.create_nn_pipeline
)


# Pipeline defined, now the device is connected to
with dai.Device(p) as device:
    # Start pipeline
    device.startPipeline()
    nn_manager.addDevice(device)
    if depth:
        q_right = device.getOutputQueue(name="right", maxSize=1, blocking=False)
        fps = FPSHandler()
    elif camera:
        q_rgb = device.getOutputQueue(name="rgb", maxSize=1, blocking=False)
        fps = FPSHandler()
    else:
        cap = cv2.VideoCapture(args.video)
        fps = FPSHandler(cap)
        seq_num = 0

    frame = None
    detections = []
    color = (255, 255, 255)

    while True:
        fps.next_iter()
        if depth:
            in_right = q_right.get()
            frame = in_right.getCvFrame()
            # Since rectified frames are horizontally flipped by default
            # if not hq:
            frame = cv2.flip(frame, 1)
            fps.tick('right')
        elif camera:
            in_rgb = q_rgb.get()
            if in_rgb is not None:
                if hq:
                    yuv = in_rgb.getData().reshape((in_rgb.getHeight() * 3 // 2, in_rgb.getWidth()))
                    frame = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
                else:
                    frame_data = in_rgb.getData().reshape(3, in_rgb.getHeight(), in_rgb.getWidth())
                    frame = np.ascontiguousarray(frame_data.transpose(1, 2, 0))
                fps.tick('rgb')
        else:
            read_correctly, vid_frame = cap.read()
            if not read_correctly:
                break

            scaled_frame = cv2.resize(vid_frame, (in_w, in_h))
            frame_nn = dai.ImgFrame()
            frame_nn.setSequenceNum(seq_num)
            frame_nn.setWidth(in_w)
            frame_nn.setHeight(in_h)
            frame_nn.setData(to_planar(scaled_frame))
            nn_manager.input.send(frame_nn)
            seq_num += 1

            # if high quality, send original frames
            frame = vid_frame if hq else scaled_frame
            fps.tick('rgb')

        in_nn = nn_manager.output.tryGetAll()
        if len(in_nn) > 0:
            if nn_manager.output_format == "detection":
                detections = in_nn[-1].detections
            for packet in in_nn:
                fps.tick('nn')

        if frame is not None:
            # if the frame is available, draw bounding boxes on it and show the frame
            for detection in detections:
                if depth: # Since rectified frames are horizontally flipped by default
                    swap = detection.xmin
                    detection.xmin = 1 - detection.xmax
                    detection.xmax = 1 - swap

                bbox = frame_norm(frame, [detection.xmin, detection.ymin, detection.xmax, detection.ymax])
                cv2.rectangle(frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (255, 0, 0), 2)
                cv2.putText(frame, nn_manager.get_label_text(detection.label), (bbox[0] + 10, bbox[1] + 20), cv2.FONT_HERSHEY_TRIPLEX, 0.5, 255)
                cv2.putText(frame, f"{int(detection.confidence * 100)}%", (bbox[0] + 10, bbox[1] + 40), cv2.FONT_HERSHEY_TRIPLEX, 0.5, 255)

                if depth: # Display coordinates as well
                    cv2.putText(frame, f"X: {int(detection.spatialCoordinates.x)} mm", (bbox[0] + 10, bbox[1] + 60), cv2.FONT_HERSHEY_TRIPLEX, 0.5, color)
                    cv2.putText(frame, f"Y: {int(detection.spatialCoordinates.y)} mm", (bbox[0] + 10, bbox[1] + 75), cv2.FONT_HERSHEY_TRIPLEX, 0.5, color)
                    cv2.putText(frame, f"Z: {int(detection.spatialCoordinates.z)} mm", (bbox[0] + 10, bbox[1] + 90), cv2.FONT_HERSHEY_TRIPLEX, 0.5, color)

            frame_fps = f"RIGHT FPS: {round(fps.tick_fps('right'), 1)}" if depth else f"RGB FPS: {round(fps.tick_fps('rgb'), 1)}"
            cv2.putText(frame, frame_fps, (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0))

            cv2.putText(frame, f"NN FPS:  {round(fps.tick_fps('nn'), 1)}", (5, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0))
            cv2.imshow("rgb", frame)

        if cv2.waitKey(1) == ord('q'):
            break
