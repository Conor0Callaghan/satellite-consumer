"""Storage module for reading and writing data to disk."""


import datetime as dt
import os
import shutil
import tempfile

import fsspec
import numpy as np
import pyresample
import xarray as xr
import yaml
import zarr
from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper
from fsspec.implementations.local import LocalFileSystem
from loguru import logger as log


def write_to_zarr(
    da: xr.DataArray,
    path: str,
    ) -> None:
    """Write the given data array to the given zarr store.

    If a Zarr store already exists at the given path, the DataArray will be appended to it.

    Any attributes on the dataarray object are serialized to json-compatible strings.

    Args:
        da: The data array to write as a Zarr store.
        path: The path to the Zarr store to write to. Can be a local filepath or S3 URL.
    """
    fs: fsspec.AbstractFileSystem = AsyncFileSystemWrapper(LocalFileSystem(auto_mkdir=True))
    if path.startswith("s3://"):
        fs = get_s3_fs()

    mode: str = "a" if fs.exists(path) else "w"
    extra_kwargs: dict[str, object] = {
        "append_dim": "time",
    } if mode == "a" else {
        "encoding": {
            "time": {"units": "nanoseconds since 1970-01-01"},
        },
    }
    # Convert attributes to be json	serializable
    for key, value in da.attrs.items():
        if isinstance(value, dict):
            # Convert np.float32 to Python floats (otherwise yaml.dump complains)
            for inner_key in value:
                inner_value = value[inner_key]
                if isinstance(inner_value, np.floating):
                    value[inner_key] = float(inner_value)
            da.attrs[key] = yaml.dump(value)
        if isinstance(value, bool | np.bool_):
            da.attrs[key] = str(value)
        if isinstance(value, pyresample.geometry.AreaDefinition):
            da.attrs[key] = value.dump() # type:ignore
        # Convert datetimes
        if isinstance(value, dt.datetime):
            da.attrs[key] = value.isoformat()

    try:
        da = da.chunk({"time": 1, "x_geostationary": -1, "y_geostationary": -1, "variable": 1})
        da.coords["variable"] = da.coords["variable"].astype(str)
        ds: xr.Dataset = da.to_dataset(name="data", promote_attrs=True)
        _ = ds.to_zarr(
            store=zarr.storage.FsspecStore(fs=fs, path=path),
            compute=True, mode=mode, consolidated=True, **extra_kwargs,
        )
    except Exception as e:
        raise OSError(f"Error writing dataset to zarr store {path}: {e}") from e

    return None

def create_latest_zip(zarr_path: str) -> str:
    """Convert a zarr store at the given path to a zip store."""
    fs: fsspec.AbstractFileSystem = AsyncFileSystemWrapper(LocalFileSystem(auto_mkdir=True))
    if zarr_path.startswith("s3://"):
        fs = get_s3_fs()

    # Open the zarr store and write it to a zip store
    ds: xr.Dataset = xr.open_zarr(
        zarr.storage.FsspecStore(fs=fs, path=zarr_path),
        consolidated=True,
    )

    zippath: str = zarr_path.rsplit("/", 1)[0] + "/latest.zarr.zip"
    with (tempfile.NamedTemporaryFile(suffix=".zip") as fsrc, fs.open(zippath, "wb") as fdst):
        try:
            _ = ds.to_zarr(store=zarr.storage.ZipStore(path=fsrc.name, mode="w"))
            log.debug(f"Zipped existing store to '{fsrc.name}'")
            shutil.copyfileobj(fsrc, fdst, length=16 * 1024)
        except Exception as e:
            raise OSError(f"Error writing dataset to zip store '{zippath}': {e}") from e
    return zippath


def _fname_to_scantime(fname: str) -> dt.datetime:
    """Converts a filename to a datetime.

    Files are of the form:
    `MSGX-SEVI-MSG15-0100-NA-20230910221240.874000000Z-NA.nat`
    So determine the time from the first element split by '.'.
    """
    return dt.datetime.strptime(fname.split(".")[0][-14:], "%Y%m%d%H%M%S").replace(tzinfo=dt.UTC)

def get_s3_fs() -> fsspec.AbstractFileSystem:
    """Get S3 filesystem object."""
    import s3fs
    for var in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]:
        if var not in os.environ:
            raise OSError(
                "Cannot use provided S3 workdir due to missing "
                f"required authorization environment variable '{var}'",
            )
    return s3fs.S3FileSystem(
        anon=False,
        key=os.environ["AWS_ACCESS_KEY_ID"],
        secret=os.environ["AWS_SECRET_ACCESS_KEY"],
        client_kwargs={"region_name": os.getenv("AWS_REGION", "eu-west-1")},
        asynchronous=True,
    )
