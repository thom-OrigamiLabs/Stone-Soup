import numpy as np
try:
    from torchvision.ops import nms
except ImportError as error:
    # TODO
    raise ImportError(
        "Usage of PyTorch detectors requires that Pytorch is installed."
        "A quick guide on how to set this up can be found here: "
        "https://pytorch.org/get-started/locally/")\
        from error

from ._video import _VideoAsyncBoxDetector
from ..base import Property
from ..types.array import StateVector
from ..types.detection import Detection

class GenericPytorchBoxObjectWrapper():
    """
    A wrapper to be used around any box detector object.
    The call sequence is as follows:
        output = postprocess_fn(detect_fn(preprocess_fn(input)))
    The input should be an NxMx3 numpy array (i.e. an image frame)
    
    The model object and the preprocess function should include setting the 
    device and precision (e.g. .cuda() and .half()) as needed.
    
    The output of postprocess_fn should be a dict containing the following tensors:
        ['boxes'] - Bounding box corrners (x0,y0,x1,y1) as absolute floats
        ['labels'] - Class identifier that keys the category_index. Suggest int in the range 0-N.
        ['scores'] - Floats in the range  0-1
    category_index should be a dict of the form {'id':int,'name':str} keyed by the contents of 'labels'
    """
    def __init__(self):
        # By default each function will return the input as its output (i.e. the identity function)
        _identity_fn = lambda x, *args: (x,) + args if args else x
        self.model = None
        self.category_index = {} 
        self.preprocess_fn = _identity_fn
        self.detect_fn = _identity_fn
        self.postprocess_fn = _identity_fn 

class PyTorchBoxObjectDetector(_VideoAsyncBoxDetector):
    """PyTorchBoxObjectDetector

     A box object detector that generates detections of objects in the form of bounding boxes 
     from image/video frames using a generic wrapper around a Pytorch object detection model.
     
     The detections generated by the box detector have the form of bounding boxes that capture 
     the area of the frame where an object is detected. Each bounding box is represented by a 
     vector of the form ``[x, y, w, h]``, where ``x, y`` denote the relative coordinates of the 
     top-left corner, while ``w, h`` denote the relative width and height of the bounding box. 
     
     Additionally, each detection carries the following meta-data fields:

     - ``raw_box``: The raw bounding box, as generated by TensorFlow.
     - ``class``: A dict with keys ``id`` and ``name`` relating to the id and name of the 
       detection class.
     - ``score``: A float in the range ``(0, 1]`` indicating the detector's confidence.
     
     Important
     ---------
     Usage of the Torchvision detectors requires that Pytorch is installed.
     A quick guide on how to set this up can be found
     `here <https://pytorch.org/get-started/locally/>`_. 
    
    """  # noqa

    modelwrapper: GenericPytorchBoxObjectWrapper = Property(doc="A generic wrapper around any Pytorch object detection model")
    use_nms: bool = Property(doc="Use non-maxima supression.", default=False)


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Extract functions from the wrapper object
        self._preprocess_fn = self.modelwrapper.preprocess_fn
        self._detect_fn = self.modelwrapper.detect_fn
        self._postprocess_fn = self.modelwrapper.postprocess_fn
        self.category_index = self.modelwrapper.category_index
       

    def _get_detections_from_frame(self, frame):        
        inp = self._preprocess_fn(frame.pixels)
        tmp_prediction = self._detect_fn(inp)
        prediction = self._postprocess_fn(tmp_prediction)
        
        # Perform non-max supression or not
        if self.use_nms:
            keep_idx = nms(prediction["boxes"], prediction['scores'], 0.4)
        else:
            keep_idx = np.ones(prediction['scores'].size(),dtype=bool)
            
        # Extract results
        nboxes = prediction["boxes"][keep_idx].cpu().detach().numpy().clip(0,1)
        boxes = nboxes.astype(int)
        classes = prediction['labels'][keep_idx].cpu().detach().numpy()
        scores = prediction['scores'][keep_idx].cpu().detach().numpy()
        
        
        # Form detections
        detections = set()
        frame_height, frame_width, _ = frame.pixels.shape
                
        # convert to relative boxes
        nboxes[:,0] /= frame_width
        nboxes[:,1] /= frame_height
        nboxes[:,2] /= frame_width
        nboxes[:,3] /= frame_height
    
        
        for box, nbox, class_, score in zip(boxes, nboxes, classes, scores):
            metadata = {
                "raw_box": nbox, # normalised x0 y0 x1 y1
                "class": self.category_index[class_],
                "score": score,
            }
            # Transform box to be in format (x, y, w, h)
            state_vector = StateVector([box[0],
                                        box[1],
                                        (box[2] - box[0]),
                                        (box[3] - box[1])])
            detection = Detection(state_vector=state_vector,
                                  timestamp=frame.timestamp,
                                  metadata=metadata)
            detections.add(detection)
        
        return detections



