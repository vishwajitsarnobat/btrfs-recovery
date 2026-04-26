# main.py

from utils.superblock import parse_superblock
from utils.btree import sweep_for_orphans

def run_recovery_engine(image_file):
   # 1. Acquire Filesystem State
    sb_data = parse_superblock(image_file)
    
    if not sb_data:
        print("[!] Aborting: Could not establish filesystem state.")
        return

    # 2. Execute Orphan Sweep and Artifact Carving
    sweep_for_orphans(image_file, sb_data)

if __name__ == "__main__":
    TARGET_IMAGE = "sandbox.img"
    run_recovery_engine(TARGET_IMAGE)
