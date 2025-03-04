import shutil
import utils
import os
import time
import enum
import posixpath


from linux_command import LinuxCommand
import logging

from types import SimpleNamespace as Namespace

import json
import copy
import hashlib
import contextlib
import subprocess
import tempfile

import pytest

from nydus_anchor import NydusAnchor
from linux_command import LinuxCommand
from utils import Size, Unit
from whiteout import WhiteoutSpec
from oss import OssHelper
from backend_proxy import BackendProxy


class Backend(enum.Enum):
    OSS = "oss"
    REGISTRY = "registry"
    LOCALFS = "localfs"
    BACKEND_PROXY = "backend_proxy"

    def __str__(self):
        return self.value


class Compressor(enum.Enum):
    NONE = "none"
    LZ4_BLOCK = "lz4_block"
    GZIP = "gzip"
    ZSTD = "zstd"

    def __str__(self):
        return self.value


class RafsConf:
    """Generate nydusd working configuration file.

    A `registry` backend example:
    {
      "device": {
        "backend": {
          "type": "registry",
          "config": {
            "scheme": "http",
            "host": "localhost:5000",
            "repo": "busybox"
          }
        },'
        "mode": "direct",
        "digest_validate": false
      }
    }
    """

    def __init__(self, anchor: NydusAnchor, image: "RafsImage" = None):
        self.__conf_file_wrapper = tempfile.NamedTemporaryFile(
            mode="w+", suffix="rafs.config"
        )
        self.anchor = anchor
        self.rafs_image = image

        self._rafs_conf_default = {
            "device": {
                "backend": {
                    "type": "oss",
                    "config": {},
                }
            },
            "mode": os.getenv("PREFERRED_MODE", "direct"),
            "iostats_files": False,
            "fs_prefetch": {"enable": False},
        }

        self._device_conf = json.loads(
            json.dumps(self._rafs_conf_default), object_hook=lambda d: Namespace(**d)
        )
        self.device_conf = utils.object_to_dict(copy.deepcopy(self._device_conf))

    def path(self):
        return self.__conf_file_wrapper.name

    def set_rafs_backend(self, backend_type, **kwargs):
        b = str(backend_type)
        self._configure_rafs("device.backend.type", b)

        if backend_type == Backend.REGISTRY:
            # Manager like nydus-snapshotter can fill the repo field, so we do nothing here.
            if "repo" in kwargs:
                self._configure_rafs(
                    "device.backend.config.repo",
                    posixpath.join(self.anchor.registry_namespace, kwargs.pop("repo")),
                )

            self._configure_rafs(
                "device.backend.config.scheme",
                kwargs["scheme"] if "scheme" in kwargs else "http",
            )
            self._configure_rafs("device.backend.config.host", self.anchor.registry_url)
            self._configure_rafs(
                "device.backend.config.auth", self.anchor.registry_auth
            )

        if backend_type == Backend.OSS:
            if "prefix" in kwargs:
                self._configure_rafs(
                    "device.backend.config.object_prefix", kwargs.pop("prefix")
                )

            self._configure_rafs(
                "device.backend.config.endpoint", self.anchor.oss_endpoint
            )
            self._configure_rafs(
                "device.backend.config.access_key_id", self.anchor.oss_ak_id
            )
            self._configure_rafs(
                "device.backend.config.access_key_secret", self.anchor.oss_ak_secret
            )
            self._configure_rafs(
                "device.backend.config.bucket_name", self.anchor.oss_bucket
            )

        if backend_type == Backend.BACKEND_PROXY:
            self._configure_rafs("device.backend.type", "registry")
            self._configure_rafs(
                "device.backend.config.scheme",
                "http",
            )
            self._configure_rafs("device.backend.config.repo", "nydus")
            self._configure_rafs(
                "device.backend.config.host", self.anchor.backend_proxy_url
            )

        if backend_type == Backend.LOCALFS:
            if "image" in kwargs:
                self._configure_rafs(
                    "device.backend.config.blob_file", kwargs.pop("image").localfs_backing_blob
                )
            else:
                self._configure_rafs(
                    "device.backend.config.dir", self.anchor.localfs_workdir
                )

        return self

    def get_rafs_backend(self):
        return self._device_conf.device.backend.type

    def set_registry_repo(self, repo):
        self._configure_rafs("device.backend.config.repo", repo)

    def _configure_rafs(self, k: str, v):
        exec("self._device_conf." + k + "=v")

    def enable_files_iostats(self):
        self._device_conf.iostats_files = True
        return self

    def enable_latest_read_files(self):
        self._device_conf.latest_read_files = True
        return self

    def enable_access_pattern(self):
        self._device_conf.access_pattern = True
        return self

    def enable_rafs_blobcache(self, is_compressed=False, work_dir=None):
        self._device_conf.device.cache = Namespace(
            type="blobcache",
            config=Namespace(
                work_dir=self.anchor.blobcache_dir if work_dir is None else work_dir
            ),
            compressed=is_compressed,
        )

        return self

    def enable_fs_prefetch(
        self,
        threads_count=8,
        merging_size=128 * 1024,
        bandwidth_rate=0,
        prefetch_all=False,
    ):
        self._configure_rafs("fs_prefetch.enable", True)
        self._configure_rafs("fs_prefetch.threads_count", threads_count)
        self._configure_rafs("fs_prefetch.merging_size", merging_size)
        self._configure_rafs("fs_prefetch.bandwidth_rate", bandwidth_rate)
        self._configure_rafs("fs_prefetch.prefetch_all", prefetch_all)

        return self

    def enable_validation(self):
        if int(self.anchor.fs_version) == 6:
            return self

        self._configure_rafs("digest_validate", True)
        return self

    def amplify_io(self, size):
        self._configure_rafs("amplify_io", size)
        return self

    def rafs_mem_mode(self, v):
        self._configure_rafs("mode", v)

    def enable_xattr(self):
        self._configure_rafs("enable_xattr", True)
        return self

    def dump_rafs_conf(self):
        # In case the conf is dumped more than once

        if int(self.anchor.fs_version) == 6:
            logging.warning("Rafs v6 must enable blobcache")
            self.enable_rafs_blobcache()

        self.__conf_file_wrapper.truncate(0)
        self.__conf_file_wrapper.seek(0)
        logging.info("Current rafs metadata mode *%s*", self._rafs_conf_default["mode"])
        self.device_conf = utils.object_to_dict(copy.deepcopy(self._device_conf))
        json.dump(self.device_conf, self.__conf_file_wrapper)
        self.__conf_file_wrapper.flush()


class RafsImage(LinuxCommand):
    def __init__(
        self,
        anchor: NydusAnchor,
        source,
        bootstrap_name=None,
        blob_name=None,
        compressor=None,
        clear_from_oss=True,
    ):
        """
        :rootfs: A plain directory from which to build rafs images(bootstrap and blob).
        :bootstrap_name: Name the generated test purpose bootstrap file.
        :blob_prefix: Generally, a sha256 string follows this prefix.
        :opts: Specify extra build options.
        :parent_image: Associate an parent image which will be created ahead of time if necessary.

        A rebuilt image tries to reuse block mapping info from parent image(bootstrap) if
        the same block resides in parent image, which means new blob file will not have the
        same block.
        """
        self.__rootfs = source
        self.bootstrap_name = (
            bootstrap_name
            if bootstrap_name is not None
            else tempfile.NamedTemporaryFile(suffix="bootstrap").name
        )
        # The file name of blob file locally.
        self.blob_name = (
            blob_name
            if blob_name is not None
            else tempfile.NamedTemporaryFile(suffix="blob").name
        )
        # blob_id is used to identify blobs residing in OSS and how a IO can access backend.
        self.blob_id = None
        self.opts = ""
        self.test_dir = os.getcwd()
        self.anchor = anchor
        LinuxCommand.__init__(self, anchor.image_bin)
        self.param_value_prefix = " "
        self.clear_from_oss = False
        self.created = False
        self.compressor = compressor
        self.clear_from_oss = clear_from_oss
        self.backend_type = None
        # self.blob_abs_path = tempfile.TemporaryDirectory(
        #     "blob", dir=self.anchor.workspace
        # ).name
        self.blob_abs_path = tempfile.NamedTemporaryFile(
            prefix="blob", dir=self.anchor.workspace
        ).name

    def rootfs(self):
        return self.__rootfs

    def _tweak_build_command(self):
        """
        Add more options into command line per as different test case configuration.
        """
        for key, value in self.command_param_dict.items():
            self.opts += (
                f"{self.param_separator}{self.param_name_prefix}"
                f"{key}{self.param_value_prefix}{value}"
            )
        for flag in self.command_flags:
            self.opts += f"{self.param_separator}{self.param_name_prefix}{flag}"

    def set_backend(self, type: Backend, **kwargs):
        self.backend_type = type

        if type == Backend.LOCALFS:
            if not os.path.exists(self.anchor.localfs_workdir):
                os.mkdir(self.anchor.localfs_workdir)
            self.set_param("blob-dir", self.anchor.localfs_workdir)
            return self
        elif type == Backend.OSS:
            self.set_param("blob", self.blob_abs_path)
            prefix = kwargs.pop("prefix", None)
            self.oss_helper = OssHelper(
                self.anchor.ossutil_bin,
                self.anchor.oss_endpoint,
                self.anchor.oss_bucket,
                self.anchor.oss_ak_id,
                self.anchor.oss_ak_secret,
                prefix,
            )
        elif self.backend_type == Backend.BACKEND_PROXY:
            self.set_param("blob", self.blob_abs_path)
        elif type == Backend.REGISTRY:
            # Let nydusify upload blob from the path, which is an intermediate file
            self.set_param("blob", self.blob_abs_path)
            pass

        return self

    def create_image(
        self,
        image_bin=None,
        parent_image=None,
        clear_from_oss=True,
        oss_uploader="util",
        compressor=None,
        prefetch_policy=None,
        prefetch_files="",
        from_stargz=False,
        fs_version=None,
        disable_check=False,
        chunk_size=None,
    ) -> "RafsImage":
        """
        :layers: Create an image on top of an existed one
        :oss_uploader: ['util', 'nydusify']. Let image builder itself upload blob to oss or use third-party oss util
        """
        self.clear_from_oss = clear_from_oss
        self.oss_uploader = oss_uploader
        self.compressor = compressor
        self.parent_image = parent_image

        assert oss_uploader in ("util", "builder", "none")
        if prefetch_policy is not None:
            self.set_param("prefetch-policy", prefetch_policy)

        self.set_param("log-level", self.anchor.log_level)

        if disable_check:
            self.set_flags("disable-check")

        if fs_version is not None:
            self.set_param("fs-version", fs_version)
        else:
            self.set_param("fs-version", str(self.anchor.fs_version))

        if self.compressor is not None:
            self.set_param("compressor", str(self.compressor))

        if chunk_size is not None:
            self.set_param("chunk-size", str(hex(chunk_size)))

        builder_output_json = tempfile.NamedTemporaryFile("w+", suffix="output.json")
        self.set_param("output-json", builder_output_json.name)
        builder_output_json.flush()

        # In order to support specify different versions of nydus image tool
        if image_bin is None:
            image_bin = self.anchor.image_bin

        # Once it's a layered image test, create test parent layer first.
        # TODO: Perhaps, should not create parent together so we can have
        # images with different flags and opts

        if self.parent_image is not None:
            self.set_param("parent-bootstrap", self.parent_image.bootstrap_name)

        if from_stargz:
            self.set_param("source-type", "stargz_index")

        # Just before beginning building image, tweak building parameters
        self._tweak_build_command()

        cmd = f"{image_bin} create --bootstrap {self.bootstrap_name} {self.opts} {self.__rootfs}"
        with utils.timer("Basic rafs image creation time"):
            _, p = utils.run(
                cmd,
                False,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=self.anchor.logging_file,
                stderr=self.anchor.logging_file,
            )
            if prefetch_policy is not None:
                p.communicate(input=prefetch_files)
            p.wait()
        assert p.returncode == 0
        assert os.path.exists(os.path.join(self.test_dir, self.bootstrap_name))

        self.created = True

        self.blob_id = json.load(builder_output_json)["blobs"][-1]
        logging.info("Generated blob id %s", self.blob_id)

        self.bootstrap_path = os.path.abspath(self.bootstrap_name)

        if self.backend_type == Backend.OSS:
            # self.blob_id = self.calc_blob_sha256(self.blob_abs_path)
            # nydus-rs image builder can also upload image itself.
            if self.oss_uploader == "util":
                self.oss_helper.upload(self.blob_abs_path, self.blob_id)
        elif self.backend_type == Backend.BACKEND_PROXY:
            shutil.copy(
                self.blob_abs_path,
                os.path.join(self.anchor.backend_proxy_blobs_dir, self.blob_id),
            )
        elif self.backend_type == Backend.LOCALFS:
            self.localfs_backing_blob = os.path.join(self.anchor.localfs_workdir, self.blob_id)

        self.anchor.put_dustbin(self.bootstrap_name)
        # Only oss has a temporary place to hold blob
        try:
            self.anchor.put_dustbin(self.blob_abs_path)
        except AttributeError:
            pass

        try:
            self.anchor.put_dustbin(self.localfs_backing_blob)
        except AttributeError:
            pass

        if self.oss_uploader == "util":
            self.dump_image_summary()

        return self

    def whiteout_spec(self, spec: WhiteoutSpec):
        self.set_param("whiteout-spec", str(spec))
        return self

    def clean_up(self):
        # In case image was not successfully created.
        if hasattr(self, "bootstrap_path"):
            os.unlink(self.bootstrap_path)

        if hasattr(self, "oss_blob_abs_path"):
            os.unlink(self.blob_abs_path)

        if hasattr(self, "localfs_backing_blob"):
            # Backing blob may already be put into dustbin.
            try:
                os.unlink(self.localfs_backing_blob)
            except FileNotFoundError:
                pass

        try:
            os.unlink(self.blob_abs_path)
        except FileNotFoundError:
            pass
        except AttributeError:
            # In case that test rootfs is not successfully scratched.
            pass

        try:
            os.unlink(self.parent_blob)
            os.unlink(self.parent_bootstrap)
        except FileNotFoundError:
            pass
        except AttributeError:
            pass

        try:
            if self.clear_from_oss and self.backend_type == Backend.OSS:
                self.oss_helper.rm(self.blob_id)
        except AttributeError:
            pass

    @staticmethod
    def calc_blob_sha256(blob):
        """Example: blob id: sha256:a810724c8b2cc9bd2a6fa66d92ced9b429120017c7cf2ef61dfacdab45fa45ca"""
        # We calculate the blob sha256 ourselves.
        sha256 = hashlib.sha256()
        with open(blob, "rb") as f:
            for block in iter(lambda: f.read(4096), b""):
                sha256.update(block)

        return sha256.hexdigest()

    def dump_image_summary(self):
        return
        logging.info(
            f"""Image summary:\t
            blob: {self.blob_name}\t
            bootstrap: {self.bootstrap_name}\t
            blob_sha256: {self.blob_id}\t
            rootfs: {self.rootfs}\t
            parent_rootfs: {self.parent_image.rootfs if self.__layers else 'Not layered image'}
            compressor: {self.compressor}\t
            blob_size: {os.stat(self.blob_abs_path).st_size//1024}KB, {os.stat(self.blob_abs_path).st_size}Bytes
            """
        )


class RafsMountParam(LinuxCommand):
    """
    Example:
        nydusd --config config.json --bootstrap bs.test --sock \
            vhost-user-fs.sock --apisock test_api --log-level trace
    """

    def __init__(self, command_name):
        LinuxCommand.__init__(self, command_name)
        self.param_name_prefix = "--"

    def bootstrap(self, bootstrap_file):
        return self.set_param("bootstrap", bootstrap_file)

    def config(self, config_file):
        return self.set_param("config", config_file)

    def sock(self, vhost_user_sock):
        return self.set_param("sock", vhost_user_sock)

    def log_level(self, log_level):
        return self.set_param("log-level", log_level)

    def mountpoint(self, path):
        return self.set_param("mountpoint", path)


class NydusDaemon(utils.ArtifactProcess):
    def __init__(
        self,
        anchor: NydusAnchor,
        image: RafsImage,
        conf: RafsConf,
        with_defaults=True,
        bin=None,
        mode="fuse",
    ):
        """Start up nydusd and mount rafs.
        :image: If image is `None`, then no `--metadata` will be passed to nydusd.
                In this case, we have to use API to mount rafs.
        """
        anchor.nydusd = self  # So pytest has a chance to clean up dirties.
        self.anchor = anchor
        self.rafs_image = image  # Associate with a rafs image to boot up.
        self.conf: RafsConf = conf
        self.mountpoint = anchor.mountpoint  # To which point nydus will mount
        self.param_value_prefix = " "
        self.params = RafsMountParam(anchor.nydusd_bin if bin is None else bin)
        self.params.set_subcommand(mode)
        if with_defaults:
            self._set_default_mount_param()

    def __str__(self):
        return str(self.params)

    def __call__(self):
        return self.params

    def _set_default_mount_param(self):
        # Set default part
        self.apisock("api_sock").log_level(self.anchor.log_level)
        if self.conf is not None:
            self.params.mountpoint(self.mountpoint).config(self.conf.path())

        if self.rafs_image is not None:
            self.params.bootstrap(self.rafs_image.bootstrap_path)

    def _wait_for_mount(self, test_fn=os.path.ismount):
        elapsed = 0
        while elapsed < 300:
            if test_fn(self.mountpoint):
                return True
            if self.p.poll() is not None:
                pytest.fail("file system process terminated prematurely")
            elapsed -= 1
            time.sleep(0.01)
        pytest.fail("mountpoint failed to come up")

    def thread_num(self, num):
        self.params.set_param("thread-num", str(num))
        return self

    def fscache_thread_num(self, num):
        self.params.set_param("fscache-threads", str(num))
        return self

    def set_fscache(self):
        self.params.set_param("fscache", self.anchor.fscache_dir)
        return self

    def log_level(self, level):
        self.params.log_level(level)
        return self

    def prefetch_files(self, file_path: str):
        self.params.set_param("prefetch-files", file_path)
        return self

    def shared_dir(self, shared_dir):
        self.params.set_param("shared-dir", shared_dir)
        return self

    def set_mountpoint(self, mp):
        self.params.set_param("mountpoint", mp)
        self.mountpoint = mp
        return self

    def supervisor(self, path):
        self.params.set_param("supervisor", path)
        return self

    def id(self, daemon_id):
        self.params.set_param("id", daemon_id)
        return self

    def upgrade(self):
        self.params.set_flags("upgrade")
        return self

    def failover_policy(self, p):
        self.params.set_param("failover-policy", p)
        return self

    def apisock(self, apisock):
        self.params.set_param("apisock", apisock)
        self.__apisock = apisock
        self.anchor.put_dustbin(apisock)
        return self

    def get_apisock(self):
        return self.__apisock

    def bootstrap(self, b):
        self.params.set_param("bootstrap", b)
        return self

    def mount(self, limited_mem=False, wait_mount=True, dump_config=True):
        """
        :limited_mem: Unit is KB, limit nydusd process virtual memory usage thus to
                     inject some faults.
        """
        cmd = str(self).split()
        self.anchor.checker_sock = self.get_apisock()

        if dump_config and self.conf is not None:
            self.conf.dump_rafs_conf()

        if isinstance(limited_mem, Size):
            limit_kb = limited_mem.B // Size(1, Unit.KB).B
            cmd = f"ulimit -v {limit_kb};" + cmd

        _, p = utils.run(
            cmd,
            False,
            shell=False,
            stdout=self.anchor.logging_file,
            stderr=self.anchor.logging_file,
        )
        self.p = p

        if wait_mount:
            self._wait_for_mount()

        return self

    def start(self):
        cmd = str(self).split()
        _, p = utils.run(
            cmd,
            False,
            shell=False,
            stdout=self.anchor.logging_file,
            stderr=self.anchor.logging_file,
        )
        self.p = p
        return self

    def wait_mount(self):
        self._wait_for_mount()

    @contextlib.contextmanager
    def automatic_mount_umount(self):
        self.mount()
        yield
        self.umount()

    def umount(self):
        """
        Umount is sometimes invoked during teardown. So it can't assert.
        """
        self._catcher_dead = True
        ret, _ = utils.execute(["umount", "-l", self.mountpoint], print_output=True)
        assert ret
        # self.p.wait()
        # assert self.p.returncode == 0

    def is_mounted(self):
        def _costum(self):
            _, output = utils.execute(
                ["cat", "/proc/mounts"], print_output=False, print_cmd=False
            )
            mounts = output.split("\n")

            for m in mounts:
                if self.mountpoint in m:
                    return True

            return False

        check_fn = os.path.ismount
        return check_fn(self.mountpoint)

    def shutdown(self):
        if self.is_mounted():
            self.umount()

        logging.error("shutting down nydusd")

        self.p.terminate()
        self.p.wait()
        assert self.p.returncode == 0


BLOB_CONF_TEMPLATE = """
{
  "type": "bootstrap",
  "id": "5a74e7f26a2970c36ffd8963a278ea11e1fd752705a13c2ec0cb20b40e2a6699",
  "domain_id": "5a74e7f26a2970c36ffd8963a278ea11e1fd752705a13c2ec0cb20b40e2a6699",
  "config": {
    "id": "5a74e7f26a2970c36ffd8963a278ea11e1fd752705a13c2ec0cb20b40e2a6699",
    "backend_type": "registry",
    "backend_config": {
      "readahead": false,
      "host": "hub.byted.org",
      "repo": "gechangwei/java",
      "auth": "",
      "scheme": "http",
      "proxy": {
        "fallback": false
      }
    },
    "cache_type": "fscache",
    "cache_config": {
      "work_dir": "/var/lib/containerd-nydus-grpc/snapshots/3754/fs"
    },
    "metadata_path": "/var/lib/containerd-nydus-grpc/snapshots/3754/fs/image/image.boot"
  },
  "fs_prefetch": {
    "enable": false,
    "prefetch_all": false,
    "threads_count": 0,
    "merging_size": 0,
    "bandwidth_rate": 0
  }
}
"""


class BlobEntryConf:
    def __init__(self, anchor) -> None:
        self.conf_base = json.loads(
            BLOB_CONF_TEMPLATE, object_hook=lambda x: Namespace(**x)
        )
        self.anchor = anchor
        self.conf_base.config.cache_config.work_dir = self.anchor.blobcache_dir

    def set_type(self, t):
        self.conf_base.type = t
        return self

    def set_repo(self, repo):
        self.conf_base.config.repo = repo
        return self

    def set_metadata_path(self, path):
        self.conf_base.config.metadata_path = path
        return self

    def set_fsid(self, fsid):
        self.conf_base.id = fsid
        self.conf_base.domain_id = fsid
        self.conf_base.config.id = fsid
        return self

    def set_backend(self):
        self.conf_base.config.backend_config.host = self.anchor.backend_proxy_url
        self.conf_base.config.backend_config.repo = "nydus"
        return self

    def set_prefetch(self, threads_cnt=4):
        self.conf_base.fs_prefetch.enable = True
        self.conf_base.fs_prefetch.prefetch_all = True
        self.conf_base.fs_prefetch.threads_count = threads_cnt
        return self

    def dumps(self):
        return json.dumps(self.conf_base, default=vars)
