"""Video readers for Stone Soup.

This is a collection of video readers for Stone Soup, allowing quick reading
of video data/streams.
"""

import datetime
import threading
from queue import Queue
from typing import Mapping, Tuple, Sequence, Any, List
from urllib.parse import ParseResult
import time

import numpy as np
missing_packages = []
try:
    import ffmpeg
except:
    
    missing_packages.append('ffmpeg-python')
try:
    import moivepy as mpy
except:
    missing_packages.append('moviepy')
try:
    import cv2
except:
    missing_packages.append('opencv-python')

if missing_packages:
    print("Usage of video processing classes requires that the following")
    print("optional package dependencies are installed: ")
    for p in missing_packages:
        print('\t',p)
    print("This can be achieved by running ")
    print("'python -m pip install [package]'")

import time

from .base import FrameReader
from .file import FileReader
from .url import UrlReader
from ..base import Property
from ..buffered_generator import BufferedGenerator
from ..types.sensordata import ImageFrame


class VideoClipReader(FileReader, FrameReader):
    """VideoClipReader

    A simple reader that uses MoviePy_ to read video frames from a file.

    Usage of MoviePy allows for the application of clip transformations
    and effects, as per the MoviePy documentation_. Upon instantiation,
    the underlying MoviePy `VideoFileClip` instance can be accessed
    through the :attr:`~clip` class property. This can then be used
    as expected, e.g.:

    .. code-block:: python

        # Rearrange RGB to BGR
        def arrange_bgr(image):
            return image[:, :, [2, 1, 0]]

        reader = VideoClipReader("path_to_file")
        reader.clip = reader.clip.fl_image(arrange_bgr)

        for timestamp, frame in reader:
            # The generated frame.pixels will now
            # be arranged in BGR format.
            ...

    .. _MoviePy: https://zulko.github.io/moviepy/index.html
    .. _documentation: https://zulko.github.io/moviepy/getting_started/effects.html
     """  # noqa:E501
    start_time: datetime.timedelta = Property(
        doc="Start time expressed as duration from the start of the clip",
        default=datetime.timedelta(seconds=0))
    end_time: datetime.timedelta = Property(
        doc="End time expressed as duration from the start of the clip",
        default=None)
    timestamp: datetime.datetime = Property(
        doc="Timestamp given to the first frame",
        default=None)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        end_time_sec = self.end_time.total_seconds() if self.end_time is not None else None
        self.clip = mpy.VideoFileClip(str(self.path)) \
            .subclip(self.start_time.total_seconds(), end_time_sec)

    @BufferedGenerator.generator_method
    def frames_gen(self):
        if self.timestamp is None:
            self.timestamp = datetime.datetime.now()
        start_time = self.timestamp
        for timestamp_sec, pixels in self.clip.iter_frames(with_times=True):
            timestamp = start_time + datetime.timedelta(seconds=timestamp_sec)
            frame = ImageFrame(pixels, timestamp)
            yield timestamp, frame


class FFmpegVideoStreamReader(UrlReader, FrameReader):
    """ FFmpegVideoStreamReader

    A threaded reader that uses ffmpeg-python_ to read frames from video
    streams (e.g. RTSP) in real-time.


    Notes
    -----
    - Use of this class requires that FFmpeg_ is installed on the host machine.
    - By default, FFmpeg performs internal buffering of frames leading to a \
    slight delay in the incoming frames (0.5-1 sec). To remove the delay it \
    is recommended to set ``input_opts={'threads': 1, 'fflags': 'nobuffer'}`` \
    when instantiating a reader, e.g: .

    .. code-block:: python

        video_reader = FFmpegVideoStreamReader('rtsp://192.168.0.10:554/1/h264minor',
                                               input_opts={'threads': 1, 'fflags': 'nobuffer'})
        for timestamp, frame in video_reader:
            ....

    .. _ffmpeg-python: https://github.com/kkroening/ffmpeg-python
    .. _FFmpeg: https://www.ffmpeg.org/download.html

    """

    url: ParseResult = Property(
        doc="Input source to read video stream from, passed as input url argument. This can "
            "include any valid FFmpeg input e.g. rtsp URL, device name when using 'dshow'/'v4l2'")
    buffer_size: int = Property(
        default=1,
        doc="Size of the frame buffer. The frame buffer is used to cache frames in cases where "
            "the stream generates frames faster than they are ingested by the reader. If "
            "`buffer_size` is less than or equal to zero, the buffer size is infinite.")
    input_opts: Mapping[str, str] = Property(
        default=None,
        doc="FFmpeg input options, provided in the form of a dictionary, whose keys correspond to "
            "option names. (e.g. ``{'fflags': 'nobuffer'}``). The default is ``{}``.")
    output_opts: Mapping[str, str] = Property(
        default=None,
        doc="FFmpeg output options, provided in the form of a dictionary, whose keys correspond "
            "to option names. The default is ``{'f': 'rawvideo', 'pix_fmt': 'rgb24'}``.")
    filters: Sequence[Tuple[str, Sequence[Any], Mapping[Any, Any]]] = Property(
        default=None,
        doc="FFmpeg filters, provided in the form of a list of filter name, sequence of "
            "arguments, mapping of key/value pairs (e.g. ``[('scale', ('320', '240'), {})]``). "
            "Default `None` where no filter will be applied. Note that :attr:`frame_size` may "
            "need to be set in when video size changed by filter.")
    frame_size: Tuple[int, int] = Property(
        default=None,
        doc="Tuple of frame width and height. Default `None` where it will be detected using "
            "`ffprobe` against the input, but this may yield wrong width/height (e.g. when "
            "filters are applied), and such this option can be used to override.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.input_opts is None:
            self.input_opts = {}
        if self.output_opts is None:
            self.output_opts = {'f': 'rawvideo', 'pix_fmt': 'rgb24'}
        if self.filters is None:
            self.filters = []

        self.buffer = Queue(maxsize=self.buffer_size)

        if self.frame_size is not None:
            self._stream_info = {
                'width': self.frame_size[0],
                'height': self.frame_size[1]}
        else:
            # Probe stream information
            self._stream_info = next(
                s
                for s in ffmpeg.probe(self.url.geturl(), **self.input_opts)['streams']
                if s['codec_type'] == 'video')

        # Initialise stream
        self.stream = ffmpeg.input(self.url.geturl(), **self.input_opts)
        for filter_ in self.filters:
            filter_name, filter_args, filter_kwargs = filter_
            self.stream = self.stream.filter(
                filter_name, *filter_args, **filter_kwargs
            )
        self.stream = (
            self.stream
            .output('pipe:', **self.output_opts)
            .global_args('-y', '-loglevel', 'panic')
            .run_async(pipe_stdout=True)
        )

        # Initialise capture thread
        self._capture_thread = threading.Thread(target=self._run)
        self._capture_thread.daemon = True
        self._capture_thread.start()

    @BufferedGenerator.generator_method
    def frames_gen(self):
        while self._capture_thread.is_alive():
            # if not self.buffer.empty():
            frame = self.buffer.get()
            timestamp = frame.timestamp
            yield timestamp, frame

    def _run(self):
        while self.stream.poll() is None:
            width = int(self._stream_info['width'])
            height = int(self._stream_info['height'])

            # Read bytes from stream
            t0 = time.time()
            in_bytes = self.stream.stdout.read(width * height * 3)
            print('Frame read:',np.round(time.time()-t0,2))

            if in_bytes:
                # Transform bytes to pixels
                frame_np = (
                    np.frombuffer(in_bytes, np.uint8)
                    .reshape([height, width, 3])
                )
                frame = ImageFrame(frame_np, datetime.datetime.now())

                # Write new frame to buffer
                self.buffer.put(frame)

    def terminate(self):
        self.stream.terminate()


class FreshestFrame(threading.Thread):
    def __init__(self, capture, name='FreshestFrame', force_frame_rate=0):
        self.capture = capture
        assert self.capture.isOpened()
        

        # this lets the read() method block until there's a new frame
        self.cond = threading.Condition()

        # this allows us to delay a read which helps for replaying video files
        # This shouldn't be set above 0 for streams
        self.force_frame_rate = force_frame_rate

        # this allows us to stop the thread gracefully
        self.running = False

        # keeping the newest frame around
        self.frame = None

        # passing a sequence number allows read() to NOT block
        # if the currently available one is exactly the one you ask for
        self.latestnum = 0

        # this is just for demo purposes        
        self.callback = None
        
        super().__init__(name=name)
        self.start()

    def start(self):
        self.running = True
        super().start()

    def release(self, timeout=None):
        self.running = False
        self.join(timeout=timeout)
        self.capture.release()
        
    def isOpened(self):
        return self.capture.isOpened()

    def run(self):
        counter = 0
        t0 = time.time()
        while self.running:
            t1 = time.time()
            dt = t1 - t0
            ddt = 1/self.force_frame_rate-dt
            if ddt > 0:
                time.sleep(ddt)

            # block for fresh frame
            (rv, img) = self.capture.read()
            counter += 1

            # publish the frame
            with self.cond: # lock the condition for this operation
                self.frame = img if rv else None
                self.latestnum = counter
                self.cond.notify_all()

            if self.callback:
                self.callback(img)

            t0 = time.time()

    def read(self, wait=True, seqnumber=None, timeout=None):
        # with no arguments (wait=True), it always blocks for a fresh frame
        # with wait=False it returns the current frame immediately (polling)
        # with a seqnumber, it blocks until that frame is available (or no wait at all)
        # with timeout argument, may return an earlier frame;
        #   may even be (0,None) if nothing received yet

        with self.cond:
            if wait:
                if seqnumber is None:
                    seqnumber = self.latestnum+1
                if seqnumber < 1:
                    seqnumber = 1
                
                rv = self.cond.wait_for(lambda: self.latestnum >= seqnumber, timeout=timeout)
                if not rv:
                    return (self.latestnum, self.frame)

            return (self.latestnum, self.frame)


class OpenCVVideoStreamReader(FrameReader):
    """ OpenCVVideoStreamReader

    A threaded reader that uses opencv-python_ to read frames from video
    streams (e.g. webcam, RTSP) in real-time, or from static video files.


    .. opencv-python: https://pypi.org/project/opencv-python/
    .. opencv: https://opencv.org/

    """


    videosource: str = Property(doc="A video file to read")
    run_async: bool = Property(default=True,doc='Run stream asynchronously')
    force_frame_rate: float = Property(default=30.0,doc='Force maximum frame rate')
    buffer_size: int = Property(
        default=1,
        doc="Size of the frame buffer. The frame buffer is used to cache frames in cases where "
            "the stream generates frames faster than they are ingested by the reader. If "
            "`buffer_size` is less than or equal to zero, the buffer size is infinite.")
    input_opts: List = Property(
        default=None,
        doc="FFmpeg input options, provided in the form of a dictionary, whose keys correspond to "
            "option names. (e.g. ``{'fflags': 'nobuffer'}``). The default is ``{}``.")
    output_opts: Mapping[str, str] = Property(
        default=None,
        doc="FFmpeg output options, provided in the form of a dictionary, whose keys correspond "
            "to option names. The default is ``{'f': 'rawvideo', 'pix_fmt': 'rgb24'}``.")
    filters: Sequence[Tuple[str, Sequence[Any], Mapping[Any, Any]]] = Property(
        default=None,
        doc="FFmpeg filters, provided in the form of a list of filter name, sequence of "
            "arguments, mapping of key/value pairs (e.g. ``[('scale', ('320', '240'), {})]``). "
            "Default `None` where no filter will be applied. Note that :attr:`frame_size` may "
            "need to be set in when video size changed by filter.")
    frame_size: Tuple[int, int] = Property(
        default=None,
        doc="Tuple of frame width and height. Default `None` where it will be detected using "
            "`ffprobe` against the input, but this may yield wrong width/height (e.g. when "
            "filters are applied), and such this option can be used to override.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.input_opts is None:
            self.input_opts = {}
        if self.output_opts is None:
            self.output_opts = {'f': 'rawvideo', 'pix_fmt': 'rgb24', 'colourTransform': cv2.COLOR_BGR2RGB}
        if self.filters is None:
            self.filters = []

        self.buffer = Queue(maxsize=self.buffer_size)

        # Initialise stream
        if self.run_async:
            self.stream = FreshestFrame(cv2.VideoCapture(self.videosource, *self.input_opts),force_frame_rate=self.force_frame_rate)
        else:
            # Don't use the FreshestFrame buffer for better compatibility
            self.stream = cv2.VideoCapture(self.videosource, *self.input_opts)

        if self.frame_size is not None:
            self._stream_info = {
                'width': self.frame_size[0],
                'height': self.frame_size[1]}
        # Initialise capture thread
        self._capture_thread = threading.Thread(target=self._run)
        self._capture_thread.daemon = True
        self._capture_thread.start()

    @BufferedGenerator.generator_method
    def frames_gen(self):
        while self._capture_thread.is_alive():
            # if not self.buffer.empty():
            frame = self.buffer.get()
            timestamp = frame.timestamp
            yield timestamp, frame

    def _run(self):
        t0 = time.time()
        while self.stream.isOpened():

            t1 = time.time()
            dt = t1 - t0
            ddt = 1/self.force_frame_rate-dt
            if ddt > 0:
                time.sleep(ddt)

            ret, frame = self.stream.read()

            if not ret:
                break
            else:
                if 'colourTransform' in self.output_opts:
                    try:
                        frame_np = cv2.cvtColor(frame, self.output_opts['colourTransform'])
                        frame = ImageFrame(frame_np, datetime.datetime.now())
                        self.buffer.put(frame)
                        
                    except:
                        # if we're out of video, put an empty frame in
                        # our main thread will catch and kill this
                        # putting in None causes the detector to fail
                        # TODO find a more graceful way of doing this
                        self.buffer.put(ImageFrame(np.zeros((10,10,3),np.uint8), datetime.datetime.now()))
        
            
            t0 = time.time()


    def terminate(self):
        self.stream.release()
