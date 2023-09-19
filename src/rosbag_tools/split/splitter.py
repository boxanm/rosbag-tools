"""ROSBag clipper class"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rosbags.interfaces import ConnectionExtRosbag1, ConnectionExtRosbag2
from rosbags.rosbag1 import Reader as Reader1
from rosbags.rosbag1 import Writer as Writer1
from rosbags.rosbag2 import Reader as Reader2
from rosbags.rosbag2 import Writer as Writer2
from tqdm import tqdm

if TYPE_CHECKING:
    from typing import Optional, Type


class InvalidTimestampError(ValueError):
    """Exception for invalid times"""

    pass


class BagSplitter:
    """Clipper: Cut a rosbag based on timestamps."""

    def __init__(
        self,
        path: Path | str,
    ) -> None:
        self._bag_start: float = None
        self._bag_end: float = None
        self._bag_duration: float = None
        self._is_ros1_reader: bool = None
        self._is_ros1_writer: bool = None
        self.inbag: Path = Path(path)

    @property
    def inbag(self) -> Path:
        """Path to input rosbag"""
        return self._inbag

    @inbag.setter
    def inbag(self, value: Path | str):
        # Check that path exists
        if not Path(value).exists():
            raise FileNotFoundError(
                f"File {value} is not an existing file. Please provide a path that exists in your file system"
            )
        self._inbag = Path(value)
        Reader = self.get_reader_class(self._inbag)
        with Reader(self._inbag) as bag:
            self._bag_start = bag.start_time
            self._bag_end = bag.end_time
            self._bag_duration = bag.duration

    @property
    def total_duration(self) -> int:
        """Duration of the bag file"""
        return self._bag_duration

    def get_reader_class(self, filename: Path | str) -> Type[Reader1 | Reader2]:
        """Return the reader class that corresponds to the filename

        Needs the filename of the rosbag to read from
        """
        is_ros1 = Path(filename).suffix == ".bag"
        self._is_ros1_reader = is_ros1
        return Reader1 if is_ros1 else Reader2

    def get_writer_class(self, filename: Path | str) -> Type[Writer1 | Writer2]:
        """Return the writer class that corresponds to the filename

        Needs the filename of the rosbag to write in
        """
        is_ros1 = Path(filename).suffix == ".bag"
        self._is_ros1_writer = is_ros1
        return Writer1 if is_ros1 else Writer2

    def delete_rosbag(self, path: Path | str):
        """Function to delete a rosbag at path `path`, to use with caution

        Args:
            path: Path to rosbag to delete.
        """
        is_ros1 = path.is_file() and path.suffix == ".bag"
        is_ros2 = path.is_dir() and len(tuple(path.glob("*.db3"))) > 0
        if is_ros1:
            path.unlink()
        elif is_ros2:
            shutil.rmtree(path)
        else:
            raise ValueError(f"Path {path} is not a valid rosbag")

    def _check_cutoff_limits(self, timestamps: [float]):
        """Check that clip limits are in the range of the bag

        Args:
            timestamps: Ros timestamps

        Raises:
            InvalidTimestampError: _raised if `timestamp` is not in the rosbag_
        """
        for ts in timestamps:
            time_ns = ts * 1e9
            if time_ns + self._bag_start < self._bag_start:
                raise InvalidTimestampError(
                    f"Split time (s: {time_ns}) should come "
                    f"after start time (e: {self._bag_start})."
                )
            if time_ns + self._bag_start > self._bag_end:
                raise InvalidTimestampError(
                    f"Split time (s: {time_ns}) should come "
                    f"before ending time (e: {self._bag_end})."
                )

    def _check_export_path(self, export_path: Path, force_out):
        if export_path == self._inbag:
            raise FileExistsError(
                f"Cannot use same file as input and output [{export_path}]"
            )
        if export_path.exists() and not force_out:
            raise FileExistsError(
                f"Path {export_path.name} already exists. "
                "Use 'force_out=True' or 'rosbag-tools clip -f' to "
                f"export to {export_path.name} even if output bag already exists."
            )
        if export_path.exists() and force_out:
            self.delete_rosbag(export_path)

    def set_writer_connections(self, writer, connections) -> {}:
        conn_map = {}
        ConnectionExt = (
            ConnectionExtRosbag1 if self._is_ros1_writer else ConnectionExtRosbag2
        )
        for conn in connections:
            if conn.topic == "/events/write_split":
                continue
            ext = cast(ConnectionExt, conn.ext)
            if self._is_ros1_writer:
                # ROS 1
                conn_map[conn.id] = writer.add_connection(
                    conn.topic,
                    conn.msgtype,
                    conn.msgdef,
                    conn.md5sum,
                    ext.callerid,
                    ext.latching,
                )
            else:
                # ROS 2
                conn_map[conn.id] = writer.add_connection(
                    conn.topic,
                    conn.msgtype,
                    serialization_format=ext.serialization_format,
                    offered_qos_profiles=ext.offered_qos_profiles,
                )
        return conn_map

    def split_rosbag(
        self,
        timestamps: [str] = None,
        outbag_path: Path | str = None,
        force_out: bool = False,
    ):
        """Clip rosbag between two elapsed times, given relative to the beginning of the rosbag

        Args:
            timestamps: Timestamps indicating where to split the bagfiles,
            outbag_path (Path | str): Path of output bag.
            force_squash (bool); Force output bag overwriting, if outbag already exists. Defaults to False.
        """
        self._check_cutoff_limits(timestamps)

        # Reader / Writer classes
        Reader = self.get_reader_class(self._inbag)
        base_path = Path(outbag_path)

        with Reader(self._inbag) as reader:
            outbag_ctr = 1
            export_path = base_path.with_name(
                base_path.stem + str(outbag_ctr) + base_path.suffix
            )
            # Check Export Path
            self._check_export_path(export_path, force_out)

            Writer = self.get_writer_class(export_path)
            if self._is_ros1_reader != self._is_ros1_writer:
                raise NotImplementedError(
                    "Rosbag conversion (ROS 1->ROS 2 / ROS 2->ROS 1) is not supported. "
                    "Use `rosbags` to convert your rosbag before using `rosbag-tools clip`."
                )

            writer = Writer(export_path)
            writer.open()
            conn_map = self.set_writer_connections(writer, reader.connections)

            timestamps = [ts * 1e9 for ts in timestamps]
            timestamps.append(self._bag_end)

            s_cliptstamp = self._bag_start
            e_cliptstamp = self._bag_start + timestamps[0]
            with tqdm(total=reader.message_count) as pbar:
                for conn, timestamp, data in reader.messages():
                    if s_cliptstamp <= timestamp <= e_cliptstamp:
                        writer.write(conn_map[conn.id], timestamp, data)
                    else:
                        outbag_ctr += 1
                        s_cliptstamp = timestamp
                        e_cliptstamp = self._bag_start + timestamps[outbag_ctr - 1]

                        export_path = base_path.with_name(
                            base_path.stem + str(outbag_ctr) + base_path.suffix
                        )
                        self._check_export_path(export_path, force_out)

                        writer.close()

                        Writer = self.get_writer_class(outbag_path)
                        writer = Writer(export_path)
                        writer.open()
                        conn_map = self.set_writer_connections(writer, reader.connections)
                    pbar.update(1)

        writer.close()
        print(f"[split] Splitting done ! Exported in {outbag_path}_[1-{outbag_ctr}]")
