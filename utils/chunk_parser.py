# utils/chunk_parser.py

import struct

def parse_chunk_map(raw_sb):
    """
    Parses the sys_chunk_array inside the Btrfs superblock.
    Returns a list of dictionaries mapping Logical to Physical addresses.
    """
    chunk_map = []
    
    # The size of the array is a 4-byte integer at offset 129
    array_size = struct.unpack("<I", raw_sb[129:133])[0]
    
    # The actual array data starts at offset 281
    pointer = 281
    end_pointer = 281 + array_size
    
    while pointer < end_pointer:
        # 1. Read the Item Key (17 bytes)
        key_data = raw_sb[pointer:pointer+17]
        obj_id, item_type, logical_start = struct.unpack("<QBQ", key_data)
        pointer += 17
        
        # Type 228 (0xE4) is the BTRFS_CHUNK_ITEM_KEY
        if item_type == 228:
            # 2. Read the Chunk Header (48 bytes)
            chunk_header = raw_sb[pointer:pointer+48]
            chunk_length = struct.unpack("<Q", chunk_header[0:8])[0]
            num_stripes = struct.unpack("<H", chunk_header[44:46])[0]
            pointer += 48
            
            # 3. Read the Stripes (Where it lives on the physical disk)
            for _ in range(num_stripes):
                # Each stripe definition is 32 bytes
                stripe_data = raw_sb[pointer:pointer+32]
                
                # The physical offset is 8 bytes in, located at index 8
                physical_start = struct.unpack("<Q", stripe_data[8:16])[0]
                pointer += 32
                
                chunk_map.append({
                    "logical_start": logical_start,
                    "logical_end": logical_start + chunk_length,
                    "physical_start": physical_start
                })
        else:
            break
            
    return chunk_map

def translate_logical_to_physical(logical_addr, chunk_map):
    """Calculates the exact physical byte offset for a given logical address."""
    for chunk in chunk_map:
        if chunk["logical_start"] <= logical_addr < chunk["logical_end"]:
            offset_inside_chunk = logical_addr - chunk["logical_start"]
            return chunk["physical_start"] + offset_inside_chunk
            
    return None # Address not found in mapped chunks
