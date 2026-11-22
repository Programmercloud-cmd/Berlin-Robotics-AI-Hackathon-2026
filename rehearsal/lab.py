"""Launch a Test Lab with a private temporary database on the rehearsal ports."""

import argparse
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from eye.lab import make_lab_app
from rehearsal.__main__ import EYE_PORT, LAB_PORT, SUPPORT_PORT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    load_dotenv(args.env_file)
    uvicorn.run(make_lab_app(eye_port=EYE_PORT, support_port=SUPPORT_PORT),
                host="127.0.0.1", port=LAB_PORT, access_log=False)


if __name__ == "__main__":
    main()
