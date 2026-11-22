"""python -m shoply.graph --format json|dot|mermaid [--out FILE]"""
import argparse
import sys

from .export import FORMATS
from .loader import load_graph

parser = argparse.ArgumentParser(description="Export the Shoply metric graph")
parser.add_argument("--format", choices=sorted(FORMATS), default="json")
parser.add_argument("--out")
args = parser.parse_args()
text = FORMATS[args.format](load_graph())
if args.out:
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
else:
    sys.stdout.write(text + "\n")
