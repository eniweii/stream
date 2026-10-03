# stream

some ai generated stuff to get data out of the stream and region file. it is stream because that is the name of the folder on my computer

## Run

Run each script from the repo root, for example:

    python common/chunk_registry.py STREAML5RA.BUN --kind stream
    python common/chunk_probe.py STREAML5RA.BUN 0x0003410D
    python flares/flare_scenery_scan.py --help

Most scanners accept `--help`. Each script has a docstring with the layout it reads.
`tools.md` has one line for every tool. `tool_gaps.md` lists what each tool does not read.
this should be removed soon because the goal is to remove any gaps and catch all data.
New shared modules go in `common/`.
