#!/usr/bin/env python3

import argparse
import contextlib
import logging
import os
import sys
import tempfile
from typing import Iterable
import zipfile

import gitignore_parser
import boto3
import botocore


class ProgramError(Exception):
    pass


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create and upload an AWS Elastic Beanstalk application version based on the current directory (and any .ebignore or .gitignore files within)."
    )
    parser.add_argument("--application", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--s3-bucket", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--existing-appver-is-error",
        help="Normally, this script will do nothing and succeed if an application version with the requested label already exists. Set this option to exit with code 1 instead.",
        action="store_true",
    )

    return parser.parse_args()


@contextlib.contextmanager
def create_zipfile():
    with tempfile.TemporaryFile() as f_raw:
        logging.debug("Created temporary file (fd: %s)", f_raw.name)
        with zipfile.ZipFile(
            f_raw, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as f:
            _add_files_to_zipfile(f)
        logging.debug("Closed zipfile")
        yield f_raw
    logging.debug("Closed raw file")


def _add_files_to_zipfile(zipf):
    if os.path.isfile(".ebignore"):
        logging.info("Using .ebignore")
        ignore_matches = gitignore_parser.parse_gitignore(".ebignore")
    elif os.path.isfile(".gitignore"):
        logging.info("Using .gitignore")
        ignore_matches = gitignore_parser.parse_gitignore(".gitignore")
    else:
        logging.info("Couldn't find .ebignore or .gitignore")
        ignore_matches = lambda x: False
    base_dir = os.getcwd()
    # We use an absolute path for scandir so the entry.path field is absolute
    for entry in _scantree(base_dir):
        relative_path = entry.path[len(base_dir) + 1 :]
        if ignore_matches(entry.path):
            logging.debug("Ignoring %s based on .ebignore", relative_path)
            continue
        logging.debug("Adding %s to zipfile", relative_path)
        zipf.write(relative_path)
    logging.info("Created zipfile with %s items", len(zipf.namelist()))


def _scantree(path) -> Iterable[os.DirEntry]:
    """Recursively yield DirEntry objects for given directory."""
    for entry in os.scandir(path):
        if entry.is_dir():
            yield from _scantree(entry.path)
        else:
            yield entry


def _log_upload_progress(bytes: int):
    logging.info("S3 upload has uploaded %s bytes...", bytes)


def upload_appver(fileobj: str, application: str, label: str, s3_bucket: str) -> str:
    s3_key = application + "/" + label + ".zip"
    logging.info(
        "Uploading appver with source fd %s to s3://%s/%s",
        fileobj.name,
        s3_bucket,
        s3_key,
    )
    s3 = boto3.resource("s3")
    s3_object = s3.Object(s3_bucket, s3_key)
    s3_object.upload_fileobj(fileobj, Callback=_log_upload_progress)

    return s3_key


def create_appver(
    application: str,
    s3_bucket: str,
    s3_key: str,
    label: str,
    description: str,
    existing_appver_is_error: bool,
):
    logging.info("Creating application version")
    eb = boto3.client("elasticbeanstalk")
    try:
        create_appver_resp = eb.create_application_version(
            ApplicationName=application,
            VersionLabel=label,
            Description=description,
            SourceBundle={"S3Bucket": s3_bucket, "S3Key": s3_key},
        )
    except botocore.exceptions.ClientError as e:
        msg = e.response["Error"]["Message"]
        if msg == "Application Version %s already exists." % label:
            if existing_appver_is_error:
                raise e
            else:
                logging.warning(
                    "The application version '%s' was created before we could upload it."
                    % label
                )


def appver_exists(application: str, label: str):
    eb = boto3.client("elasticbeanstalk")
    resp = eb.describe_application_versions(
        ApplicationName=application, VersionLabels=[label]
    )
    if len(resp["ApplicationVersions"]) > 0:
        return True
    return False


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    if appver_exists(application=args.application, label=args.label):
        if args.existing_appver_is_error:
            raise ProgramError("Application version '%s' already exists!" % args.label)
        logging.info("Application version '%s' already exists.", args.label)
        return

    with create_zipfile() as fileobj:
        fileobj.seek(0)
        s3_key = upload_appver(
            fileobj=fileobj,
            application=args.application,
            label=args.label,
            s3_bucket=args.s3_bucket,
        )

    create_appver(
        application=args.application,
        s3_bucket=args.s3_bucket,
        s3_key=s3_key,
        label=args.label,
        description=args.description,
        existing_appver_is_error=args.existing_appver_is_error,
    )
    logging.info("All done!")


if __name__ == "__main__":
    try:
        main()
    except ProgramError as e:
        print(e)
        sys.exit(1)
