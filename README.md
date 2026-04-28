# btrfs-forensics

A forensic tool for recovering deleted files from raw Btrfs disk images. It operates entirely on the raw binary image — no mounted filesystem required — by scanning for orphaned Copy-on-Write (CoW) B-tree nodes left behind after file deletion.

---

## Background

Btrfs uses a Copy-on-Write B-tree structure to manage all filesystem metadata. Whenever metadata is modified (e.g., a file is written or deleted), Btrfs does **not** overwrite the old node in place. Instead, it allocates a new node and writes the updated version there, leaving the old node intact on disk until the space is reclaimed by the garbage collector.

When a file is deleted, the corresponding B-tree leaf nodes — containing `INODE_ITEM`, `INODE_REF`, `DIR_ITEM`, `DIR_INDEX`, and `EXTENT_DATA` items — are CoW-copied and the old copies may linger in unallocated space for an indeterminate time. This tool finds and parses those lingering copies.

Additionally, Btrfs B-tree balancing and merging operations can leave **Orphan-Items**: item-pointer-sized slots in the item array of a leaf node that are beyond the `nritems` count but still contain valid-looking metadata from before the merge. These are found in both old and current-generation nodes.

### Key References

- Bhat, A. & Wani, M.A. (2018). *Forensic analysis of B-tree file system (Btrfs)*. Digital Investigation.
- Wani, M.A. et al. (2020). *An analysis of anti-forensic capabilities of B-tree file system (Btrfs)*.
- [Btrfs On-Disk Format — official documentation](https://btrfs.readthedocs.io/en/latest/dev/On-disk-format.html)

---

## How It Works

The recovery pipeline runs in four stages.

### Stage 1 — Superblock Analysis (`utils/superblock.py`, `utils/chunk_parser.py`)

The tool reads the primary superblock at offset `0x10000` (64 KiB) and validates it via the `_BHRfS_M` magic number. It extracts:

| Field | Purpose |
|---|---|
| `FSID` | Filesystem UUID — used to fingerprint nodes on disk |
| `generation` | Current transaction ID — nodes older than this are "orphaned" |
| `nodesize` | Size of every B-tree node (usually 16 KiB) |
| `root_tree_addr` | Logical address of the Root Tree root |
| `chunk_tree_addr` | Logical address of the Chunk Tree root |

**Chunk map construction** happens in two steps:

1. **Bootstrap** (`parse_chunk_map`): The `sys_chunk_array` embedded in the superblock is parsed. This contains only SYSTEM chunks — just enough to locate the chunk tree itself.
2. **Full traversal** (`parse_chunk_tree`): The chunk tree is walked recursively (internal nodes → leaf nodes), and all `CHUNK_ITEM` entries are collected. This gives the complete logical→physical mapping covering DATA and METADATA chunks too.

The chunk map is a list of `{logical_start, logical_end, physical_start, chunk_length}` entries used for address translation throughout the rest of the pipeline.

---

### Stage 2 — Raw Disk Sweep (`utils/btree.py` — `sweep_for_orphans`)

The tool iterates over the entire raw image in `nodesize`-aligned steps, starting just after the superblock region. For each block it reads the 101-byte **node header** (`btrfs_header`) and checks:

1. **FSID match**: `header[0x20:0x36]` must equal the filesystem UUID from the superblock. Blocks that don't match are skipped instantly.
2. **Generation check**:
   - `node_gen < sb_gen` → **Orphaned node** (old CoW copy). Its items are parsed with full Orphan-Item scanning enabled.
   - `node_gen == sb_gen` and `level == 0` → **Current-generation leaf**. Only Orphan-Items (slots beyond `nritems`) are scanned; valid items in live nodes are the active filesystem and not targets for recovery.

Internal nodes (`level > 0`) are skipped in the current implementation.

---

### Stage 3 — Node Item Parsing (`parse_node_items`, `_parse_single_item`)

For each identified leaf node, item pointers (`btrfs_item`, 25 bytes each) are read. Each pointer holds a `btrfs_disk_key` (object ID, item type, key offset), a `data_offset`, and a `data_size`.

#### Valid items (indices `0 … nritems-1`)

All items within the declared `nritems` range are parsed normally.

#### Orphan-Items (indices `nritems … max_possible-1`)

Slots beyond `nritems` are examined for remnants of B-tree balancing. A slot is considered a real Orphan-Item if:
- `data_size` is non-zero and fits within `nodesize`
- `data_offset + data_size` falls within the node body
- `item_type` is one of the five known recoverable types

Up to 200 additional slots beyond `nritems` are checked per node.

#### Item type handlers

| Item Type | Key byte | What is extracted |
|---|---|---|
| `INODE_ITEM` (0x01) | | Full 160-byte `btrfs_inode_item` parsed: size, nlink, uid/gid, mode, atime/ctime/mtime/otime. Stored in the report's inode metadata table. |
| `INODE_REF` (0x0C) | | Filename of the inode (the object ID of the key *is* the inode number; the key offset is the parent directory inode). Multiple refs in one item are handled. |
| `DIR_ITEM` (0x54) | | 30-byte directory entry header parsed: target inode number and filename. Updates the `inode_map`. |
| `DIR_INDEX` (0x60) | | Same structure as `DIR_ITEM`, parsed identically. |
| `EXTENT_DATA` (0x6C) | | 21-byte extent header parsed. Two sub-cases: inline (type=0) and regular (type=1). See below. |

**Inode → filename map**: `inode_map` is a dict built incrementally from `DIR_ITEM`, `DIR_INDEX`, and `INODE_REF` items. When multiple names are found for the same inode, the shorter/cleaner name is kept. This map is used to assign meaningful filenames to extracted data.

---

### File Extent Extraction

#### Inline extents (`BTRFS_FILE_EXTENT_INLINE = 0`)

The file data is stored directly inside the B-tree node, immediately after the 21-byte extent header. The payload is written to disk as:

```
<sanitized_filename>_gen<generation>_<source>_inline.bin
```

If the extent is compressed (zlib, lzo, or zstd), a warning is printed and the raw (still-compressed) bytes are saved.

#### Regular extents (`BTRFS_FILE_EXTENT_REG = 1`)

The extent header is followed by 32 bytes of extent reference data:

| Field | Size | Description |
|---|---|---|
| `disk_bytenr` | 8 | Logical address of the extent on disk |
| `disk_num_bytes` | 8 | Size of the extent on disk |
| `offset` | 8 | Byte offset within the extent where the file's data starts |
| `num_bytes` | 8 | Number of file bytes in this extent |

Sparse extents (`disk_bytenr == 0`) are logged and skipped. Non-sparse extents are queued for the second pass.

---

### Stage 4 — Second Pass: Regular Extent Extraction (`_extract_regular_extents`)

After the full sweep, all queued regular extent references are processed:

1. `disk_bytenr` is translated from logical → physical using `translate_logical_to_physical` against the chunk map.
2. The physical file is seeked to `physical_addr + offset` and `num_bytes` bytes are read.
3. Data is written to:
   ```
   <sanitized_filename>_gen<generation>_<source>_extent.bin
   ```
4. If the logical address is not covered by the chunk map, the extent is logged as failed.

---

### Stage 5 — Recovery Report (`utils/recovery_report.py`)

A `RecoveryReport` object accumulates statistics and artifacts throughout the run. At the end it:

- Prints a human-readable summary table to stdout (filename, inode, generation, extent type, size, source).
- Saves a machine-readable `recovery_report.json` to the output directory with full metadata for every recovered artifact and all inode metadata.

---

## Codebase Structure

```
btrfs-forensics/
├── main.py                    # Entry point & CLI
└── utils/
    ├── constants.py           # All Btrfs on-disk format constants and offsets
    ├── superblock.py          # Superblock parsing
    ├── chunk_parser.py        # Chunk map bootstrap + chunk tree traversal
    │                          # + logical→physical address translation
    ├── btree.py               # Raw sweep, node parsing, item handlers,
    │                          # extent extraction (inline + regular)
    ├── inode_parser.py        # btrfs_inode_item (160 bytes) parser
    └── recovery_report.py     # Statistics accumulator + JSON/text report
```

---

## Usage

```bash
# Basic usage (scans sandbox.img, writes to recovery_output/)
python main.py

# Specify a custom image and output directory
python main.py /path/to/disk.img -o /path/to/output/

# Skip scanning current-generation nodes for Orphan-Items
python main.py disk.img --no-current-gen
```

### CLI Options

| Argument | Default | Description |
|---|---|---|
| `image` | `sandbox.img` | Path to the raw Btrfs disk image |
| `-o` / `--output` | `recovery_output` | Output directory for recovered files and the JSON report |
| `--no-current-gen` | *(off)* | If set, skips current-generation nodes entirely (only scans orphaned nodes) |

### Output

After a run the output directory contains:
- `<filename>_gen<N>_<source>_inline.bin` — recovered inline-extent files
- `<filename>_gen<N>_<source>_extent.bin` — recovered regular-extent files
- `recovery_report.json` — machine-readable report with all stats and per-file metadata

The `source` field in filenames is either `valid` (item was within `nritems`) or `orphan` (item was beyond `nritems`, found by Orphan-Item scanning).

---

## Setup

Requires Python ≥ 3.14. No third-party dependencies.

```bash
# Using uv (recommended)
uv sync
uv run python main.py

# Or plain Python
python main.py
```

---

## Limitations

- **Compression**: Inline extents with zlib/lzo/zstd compression are saved in their raw compressed form; decompression is not implemented yet.
- **Internal nodes**: Internal B-tree nodes (`level > 0`) are currently skipped. Their slack space can contain leaf-node remnants from splits but is not yet scanned.
- **Multi-device / RAID**: Only single-stripe, single-device images are fully tested. RAID stripe reconstruction is not implemented.
- **Checksum validation**: Node and extent checksums are not verified; the FSID match and structural sanity checks are used instead.
- **Space reuse**: If the OS has reclaimed and overwritten an orphaned node's disk blocks, the data is gone and cannot be recovered.
