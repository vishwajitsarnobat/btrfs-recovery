# utils/btree.py

import struct
import os
from .constants import (
    SUPERBLOCK_OFFSET, NODE_HEADER_SIZE, ITEM_POINTER_SIZE, 
    BTRFS_EXTENT_DATA_KEY, BTRFS_DIR_ITEM_KEY
)

# Ensure a directory exists to dump our recovered files
OUTPUT_DIR = "recovery_output"
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

def parse_node_items(f, current_offset, node_gen, inode_map):
    """
    Parses a B-tree leaf node's item pointers. 
    It builds a mapping of Inodes to Filenames, and extracts inline file data.
    """
    f.seek(current_offset)
    header = f.read(NODE_HEADER_SIZE)
    
    # Verify it is a leaf node (Level 0 is at offset 100)
    if header[100] != 0:
        return 
        
    # Get number of items (offset 96)
    nritems = struct.unpack("<I", header[96:100])[0]
    
    for i in range(nritems):
        # Calculate offset for this specific 25-byte item pointer
        pointer_offset = current_offset + NODE_HEADER_SIZE + (i * ITEM_POINTER_SIZE)
        f.seek(pointer_offset)
        pointer_raw = f.read(ITEM_POINTER_SIZE)
        
        # Unpack the pointer
        key_raw, data_offset, data_size = struct.unpack("<17sII", pointer_raw)
        
        # Extract the Object ID (first 8 bytes of the key) and the Item Type (9th byte)
        object_id = struct.unpack("<Q", key_raw[0:8])[0]
        item_type = key_raw[8]
        
        # Calculate where the actual data payload starts
        absolute_data_offset = current_offset + NODE_HEADER_SIZE + data_offset
        
        # ---------------------------------------------------------
        # 1. CARVE FILENAMES (DIR_ITEM)
        # ---------------------------------------------------------
        if item_type == BTRFS_DIR_ITEM_KEY:
            f.seek(absolute_data_offset)
            
            # Read the 30-byte Btrfs Directory Item Header
            dir_item_header = f.read(30)
            
            # Unpack to get the Target Inode and the Name Length
            target_key, transid, data_len, name_len, file_type = struct.unpack("<17sQHHB", dir_item_header)
            
            # The target Inode is the first 8 bytes of the target_key
            target_inode = struct.unpack("<Q", target_key[0:8])[0]
            
            # Read the actual filename string
            raw_name = f.read(name_len)
            try:
                filename = raw_name.decode('utf-8', errors='ignore')
                print(f"        [DIR] Found Filename: '{filename}' -> Points to Inode {target_inode}")
                
                # UPDATE THE IN-MEMORY MAP: Store the discovered filename for this target Inode
                if target_inode not in inode_map:
                    inode_map[target_inode] = filename
            except:
                pass
                
        # ---------------------------------------------------------
        # 2. CARVE FILE DATA (EXTENT_DATA)
        # ---------------------------------------------------------
        elif item_type == BTRFS_EXTENT_DATA_KEY:
            f.seek(absolute_data_offset)
            
            # The first 21 bytes are the Btrfs extent header.
            extent_header = f.read(21)
            extent_type = extent_header[20]
            
            if extent_type == 0:
                # INLINE DATA RECOVERY (Small Files)
                payload_size = data_size - 21
                raw_file_bytes = f.read(payload_size)
                
                real_filename = inode_map.get(object_id, f"inode_{object_id}")
                out_path = f"{OUTPUT_DIR}/{real_filename}_gen_{node_gen}_inline.bin"
                with open(out_path, "wb") as out_file:
                    out_file.write(raw_file_bytes)
                
                print(f"        [***] Extracted Inline Data -> {out_path}")

            elif extent_type == 1:
                # REGULAR EXTENT RECOVERY (Large Files)
                # The next 53 bytes contain the Regular Extent data. 
                # We need the first 16 bytes: Logical Address (8 bytes) and Size (8 bytes).
                regular_extent_data = f.read(16)
                
                logical_address, extent_size = struct.unpack("<QQ", regular_extent_data)
                
                real_filename = inode_map.get(object_id, f"inode_{object_id}")
                
                print(f"        [!] Found Large File: '{real_filename}' (Inode {object_id})")
                print(f"            -> Extent Size: {extent_size} bytes")
                print(f"            -> Logical Address: {logical_address}")

def sweep_for_orphans(image_path, sb_data):
    """
    Sweeps the raw disk for valid B-tree nodes that belong to previous
    generations, effectively bypassing the active filesystem tree.
    """
    print("[*] Starting raw disk sweep for orphaned CoW nodes...")
    fsid = sb_data["fsid"]
    sb_gen = sb_data["generation"]
    nodesize = sb_data["nodesize"]
    orphans_found = 0
    
    # Initialize the global dictionary to track Inode-to-Filename mappings
    inode_map = {} 
    
    with open(image_path, "rb") as f:
        # Start immediately after the superblock
        current_offset = SUPERBLOCK_OFFSET + nodesize
        
        while True:
            f.seek(current_offset)
            header = f.read(NODE_HEADER_SIZE)
            
            if not header or len(header) < NODE_HEADER_SIZE:
                break 
                
            node_fsid = header[32:48]
            
            if node_fsid == fsid:
                # Extract generation
                node_gen = struct.unpack("<Q", header[80:88])[0]
                
                # Core forensic logic: Is it orphaned?
                if node_gen < sb_gen:
                    print(f"    [!] Orphaned Node Found at offset: {current_offset} (Gen: {node_gen})")
                    orphans_found += 1
                    
                    # Pass the map into the parser to cross-reference data and names
                    parse_node_items(f, current_offset, node_gen, inode_map)
            
            current_offset += nodesize

    print(f"\n[*] Sweep complete. Found {orphans_found} orphaned nodes.")
