import os
import subprocess
import sys
import platform
import shutil

def compile_targets(project_dir="."):
    """
    Build a CMake project.
    :param project_dir: Path to the project root directory (containing CMakeLists.txt); defaults to the current directory.
    """
    project_dir = os.path.abspath(project_dir)
    build_dir = os.path.join(project_dir, "build")
    
    targets = ["pi", "sha256", "uni"]
    current_os = platform.system()
    
    print(f"[*] Detected OS: {current_os} {platform.release()}")
    print(f"[*] Project Directory: {project_dir}")
    
    if os.path.exists(build_dir):
        print(f"[*] Existing 'build' directory detected at {build_dir}. Removing to prevent CMake generator conflicts...")
        try:
            shutil.rmtree(build_dir)
            print("[+] Successfully cleared previous build cache.")
        except Exception as e:
            print(f"[-] Error: Unable to delete the '{build_dir}' directory.")
            print("    Please ensure no other programs (e.g., VS Code, antivirus, active terminals) are utilizing these files.")
            print(f"    System Error: {e}")
            sys.exit(1)

    cmake_config_cmd = ["cmake", "-S", project_dir, "-B", build_dir]
    
    if current_os == "Windows":
        has_gpp = shutil.which("g++") is not None
        has_msvc = shutil.which("cl") is not None
        
        if has_gpp and not has_msvc:
            print("[*] Strategy: MinGW (g++) detected without an active MSVC environment.")
            print("[*] Applying generator override: -G \"MinGW Makefiles\"")
            cmake_config_cmd.extend(["-G", "MinGW Makefiles"])

    print("[*] Running CMake configuration...")

    try:
        subprocess.run(
            cmake_config_cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=project_dir
        )
        print("[+] CMake configuration completed successfully.\n")
        
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr if e.stderr else (e.stdout if e.stdout else "Unknown error")
        
        compiler_error_keywords = [
            "cmake_cxx_compiler not set",
            "no cmake_cxx_compiler could be found",
            "nmake"
        ]
        
        print("[-] Error: CMake configuration failed!")
        
        if any(keyword in error_msg.lower() for keyword in compiler_error_keywords):
            print("-" * 60)
            print("[-] System Diagnostic: C++ Compiler Not Found")
            print("    Please refer to the following documentation to set up your C++ environment:")
            print("    https://code.visualstudio.com/docs/cpp/introvideos-cpp")
            print("-" * 60)
        else:
            print(f"\nError Details:\n{error_msg}")
            
        sys.exit(1)

    success_count = 0
    for target in targets:
        print(f">>> Building target: {target}")
        try:
            subprocess.run(
                ["cmake", "--build", build_dir, "--target", target, "--config", "Release"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=project_dir
            )
            print(f"[+] Target '{target}' built successfully.")
            success_count += 1
        except subprocess.CalledProcessError as e:
            print(f"[-] Error: Failed to build target '{target}'. Skipping to the next target.")
            error_details = e.stderr.strip().splitlines()[-1] if e.stderr else 'Unknown error'
            print(f"    Error Details: {error_details}")
        print("-" * 40)

    print(f"[*] Build process finished. Successfully built {success_count}/{len(targets)} targets.")
    print(f"[*] Executables are located in the '{os.path.join(build_dir, 'bin')}' directory.")

if __name__ == "__main__":
    target_directory = sys.argv[1] if len(sys.argv) > 1 else "."
    compile_targets(target_directory)