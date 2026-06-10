import serial
import time
import threading
import queue
import sys
import os
import argparse
import json  
import math  
import subprocess
import platform

import _BurnerHub
import uuid
import tempfile

_REAL_STDOUT = sys.__stdout__
sys.stdout = open(os.devnull, 'w')

target_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'ESP32-controller'))

PRINT_LOCK = threading.Lock()
GLOBAL_START_EVENT = threading.Event()  
START_TASKS_EVENT = threading.Event()   
PRINTED_PHASES = set()                  
STOP_TRAIN_EVENT = threading.Event()    

PERMANENT_LOGS = queue.Queue()  
ACTIVE_TASKS = {}               
LAST_DYNAMIC_LINES = 0          

JUDGE_RESULTS = {}              
JUDGE_RESULTS_BY_TYPE = {}      
EXPECTED_ANSWERS = {}           

MONITOR_FILE_LOCK = threading.Lock()
MONITOR_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'monitor_status.txt'))

def write_monitor_log(message):
    try:
        with MONITOR_FILE_LOCK:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(MONITOR_FILE_PATH, 'a', encoding='utf-8') as f:
                f.write(f"[{timestamp}] {message}\n")
                f.flush()
    except Exception:
        pass

def truncate_str(s, length=55):
    if len(s) > length:
        return s[:length-3] + "..."
    return s

def smart_truncate_diff(expected, got, length=55):
    if len(expected) <= length and len(got) <= length:
        return expected, got

    diff_idx = 0
    min_len = min(len(expected), len(got))
    while diff_idx < min_len and expected[diff_idx] == got[diff_idx]:
        diff_idx += 1
        
    if diff_idx < 15:
        exp_out = expected[:length-3] + "..." if len(expected) > length else expected
        got_out = got[:length-3] + "..." if len(got) > length else got
        return exp_out, got_out
        
    context_start = diff_idx - 10
    avail_len = length - 3 
    
    def format_trunc(s):
        sub_s = s[context_start : context_start + avail_len - 3]
        res = "..." + sub_s
        if context_start + len(sub_s) < len(s):
            res += "..."
        return res
        
    return format_trunc(expected), format_trunc(got)

def get_task_category(task_str):
    if task_str.startswith('['):
        try:
            content = task_str.split(']')[0][1:] 
            base = content.split('-')[0] 
            return f"[{base}]"
        except Exception:
            pass
    return "[unknown]"

TRAIN_WIDTH = 68   
WINDOW_WIDTH = 71  

ANIM_STATE = 'STEAM_RIGHT'
TRAIN_POS = -TRAIN_WIDTH  
WIRE_LEN = 0

TRAIN_ART_STEAM_RIGHT = [
    r"                                           @  @@  (@@) (@@)         ",
    r"  _______________   _______________       _______________||______   ",
    r" |               | |               |     |      | |  |  [__]   | \  ",
    r" |NOT CRASHED :D |-| JUDGE WORKING |-----|      |-|  |         | |  ",
    r" |_______________| |_______________|     |______|_|__|_________|_|  ",
    r"   (O)-------(O)     (O)-------(O)       (O)--(O)    \__(O)_(O)_/   "
]

TRAIN_ART_ELECTRIC_LEFT = [
    r"        /||\                                  /||\                  ",
    r"  _______||_______________   __________________||_________________  ",
    r" /     [____]             | |                [____]               | ",
    r"/====== NO DELAY          |-| HIGH SPEED TEST  | ZERO PACKET LOSS | ",
    r"|_________________________| |__________________|__________________| ",
    r"   (O)--------------(O)       (O)----------(O)    (O)---------(O)   "
]

CURRENT_TRAIN_ART = TRAIN_ART_STEAM_RIGHT

def get_train_frame(i):
    line = CURRENT_TRAIN_ART[i]
    if TRAIN_POS < 0:
        visible_train = line[-TRAIN_POS:] 
        frame = f"{visible_train:<{WINDOW_WIDTH}}"[:WINDOW_WIDTH]
    else:
        padded = (" " * TRAIN_POS) + line
        frame = f"{padded:<{WINDOW_WIDTH}}"[:WINDOW_WIDTH]
        
    if i == 0 and WIRE_LEN > 0:
        wire_char = "="
        if ANIM_STATE in ['DRAW_WIRE', 'ELECTRIC_LEFT']:
            wire_bg = (wire_char * WIRE_LEN).ljust(WINDOW_WIDTH, " ")
        else:
            wire_bg = (wire_char * WIRE_LEN).rjust(WINDOW_WIDTH, " ")
            
        merged = ""
        for j in range(WINDOW_WIDTH):
            c_frame = frame[j] if j < len(frame) else " "
            c_wire  = wire_bg[j] if j < len(wire_bg) else " "
            merged += c_frame if c_frame != " " else c_wire
        frame = merged
        
    return frame

def update_train_state():
    global TRAIN_POS, CURRENT_TRAIN_ART, ANIM_STATE, WIRE_LEN
    if ANIM_STATE == 'STEAM_RIGHT':
        TRAIN_POS += 1
        if TRAIN_POS >= WINDOW_WIDTH:
            ANIM_STATE = 'DRAW_WIRE'
            CURRENT_TRAIN_ART = TRAIN_ART_ELECTRIC_LEFT
            
    elif ANIM_STATE == 'DRAW_WIRE':
        WIRE_LEN += 4
        if WIRE_LEN >= WINDOW_WIDTH:
            WIRE_LEN = WINDOW_WIDTH
            ANIM_STATE = 'ELECTRIC_LEFT'
            TRAIN_POS = WINDOW_WIDTH
            
    elif ANIM_STATE == 'ELECTRIC_LEFT':
        TRAIN_POS -= 1
        if TRAIN_POS <= -TRAIN_WIDTH:
            ANIM_STATE = 'ERASE_WIRE'
            CURRENT_TRAIN_ART = TRAIN_ART_STEAM_RIGHT
            
    elif ANIM_STATE == 'ERASE_WIRE':
        WIRE_LEN -= 4
        if WIRE_LEN <= 0:
            WIRE_LEN = 0
            ANIM_STATE = 'STEAM_RIGHT'
            TRAIN_POS = -TRAIN_WIDTH

def redraw_screen():
    global LAST_DYNAMIC_LINES
    
    if LAST_DYNAMIC_LINES > 1:
        _REAL_STDOUT.write(f"\r\033[{LAST_DYNAMIC_LINES - 1}A")
    elif LAST_DYNAMIC_LINES == 1:
        _REAL_STDOUT.write("\r")
        
    while not PERMANENT_LOGS.empty():
        _REAL_STDOUT.write(f"\033[2K\r{PERMANENT_LOGS.get()}\n")
        
    dynamic_output = []
    temp_printed_phases = set(PRINTED_PHASES)
    
    for port in sorted(ACTIVE_TASKS.keys()):
        task = ACTIVE_TASKS[port]
        phase = task['phase']
        
        if phase not in temp_printed_phases:
            temp_printed_phases.add(phase)
            display_phase = phase
        else:
            display_phase = ""
            
        dynamic_output.append(f"| {display_phase:<32} | {port:<8} | {task['result']:<21} |")
        
    if not STOP_TRAIN_EVENT.is_set():
        TRAIN_HEIGHT = len(CURRENT_TRAIN_ART)
        for i in range(TRAIN_HEIGHT):
            dynamic_output.append(get_train_frame(i))
            
    dynamic_lines = len(dynamic_output)
    
    for i, line in enumerate(dynamic_output):
        _REAL_STDOUT.write(f"\033[2K\r{line}")
        if i < dynamic_lines - 1:
            _REAL_STDOUT.write("\n")
            
    if dynamic_lines == 0:
        if LAST_DYNAMIC_LINES > 0:
            _REAL_STDOUT.write("\033[2K\r") 
            for _ in range(LAST_DYNAMIC_LINES - 1):
                _REAL_STDOUT.write("\n\033[2K\r")
            if LAST_DYNAMIC_LINES - 1 > 0:
                _REAL_STDOUT.write(f"\033[{LAST_DYNAMIC_LINES - 1}A")
    else:
        leftover = LAST_DYNAMIC_LINES - dynamic_lines
        if leftover > 0:
            for _ in range(leftover):
                _REAL_STDOUT.write("\n\033[2K\r")
            _REAL_STDOUT.write(f"\033[{leftover}A")
            
    LAST_DYNAMIC_LINES = dynamic_lines
    _REAL_STDOUT.flush()

def train_animation():
    while not STOP_TRAIN_EVENT.is_set():
        update_train_state()
        with PRINT_LOCK:
            redraw_screen()
        time.sleep(0.08)

def tty_print(message):
    with PRINT_LOCK:
        PERMANENT_LOGS.put(message)
        redraw_screen()

def tty_print_row(phase, port, result):
    with PRINT_LOCK:
        if phase not in PRINTED_PHASES:
            PRINTED_PHASES.add(phase)
            display_phase = phase
        else:
            display_phase = "" 
        formatted_msg = f"| {display_phase:<32} | {port:<8} | {result:<21} |"
        PERMANENT_LOGS.put(formatted_msg)
        redraw_screen()

def tty_begin_task(phase, port, running_msg):
    with PRINT_LOCK:
        ACTIVE_TASKS[port] = {
            "phase": phase,
            "result": running_msg
        }
        redraw_screen()

def tty_end_task(port, final_result):
    with PRINT_LOCK:
        if port in ACTIVE_TASKS:
            task = ACTIVE_TASKS.pop(port)
            phase = task["phase"]
            
            if phase not in PRINTED_PHASES:
                PRINTED_PHASES.add(phase)
                display_phase = phase
            else:
                display_phase = ""
                
            formatted_msg = f"| {display_phase:<32} | {port:<8} | {final_result:<21} |"
            PERMANENT_LOGS.put(formatted_msg)
        redraw_screen()

def read_lines_from_file(filepath):
    if not os.path.exists(filepath):
        return []
    lines = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            clean_line = line.strip()
            if clean_line and not clean_line.startswith("#"):
                lines.append(clean_line)
    return lines

def run_local_tests(data_cache):
    stage1_score = 0
    stage1_passed = True
    stage1_details = {}
    stage1_errors = []

    targets = {'pi': 25, 'sha256': 25, 'uni': 50}
    ext = ".exe" if platform.system() == "Windows" else ""
    phase_0_name = '[Phase 0] Algorithm Evaluation'

    for target, max_pts in targets.items():
        tty_begin_task(phase_0_name, target, 'TESTING...')
        write_monitor_log(f"[Stage 1] [Start] Target: {target}")
        exe_path = os.path.abspath(os.path.join("../src/EZ-code/build", "bin", target + ext))
        in_key = target + ".in"
        out_key = target + ".out"

        if not os.path.exists(exe_path) or in_key not in data_cache or out_key not in data_cache:
            stage1_details[target] = {'score': 0, 'max': max_pts, 'status': 'FILE MISSING'}
            stage1_passed = False
            tty_end_task(target, 'FAIL')
            write_monitor_log(f"[Stage 1] [Finished] Target: {target}, Result: FAIL (FILE MISSING)")
            continue

        try:
            input_data = data_cache[in_key]
            expected_out = data_cache[out_key].strip()

            with tempfile.TemporaryDirectory() as tmpdir:
                result = subprocess.run([exe_path], input=input_data, text=True, capture_output=True, timeout=10.0, cwd=tmpdir)
            got_out = result.stdout.strip()

            if got_out == expected_out:
                stage1_score += max_pts
                stage1_details[target] = {'score': max_pts, 'max': max_pts, 'status': 'PASS'}
                tty_end_task(target, 'PASS')
                write_monitor_log(f"[Stage 1] [Finished] Target: {target}, Result: PASS")
            else:
                stage1_details[target] = {'score': 0, 'max': max_pts, 'status': 'WRONG ANSWER'}
                stage1_passed = False
                stage1_errors.append({
                    'task': f"Local Target: {target}",
                    'expected': repr(expected_out)[1:-1],
                    'got': repr(got_out)[1:-1]
                })
                tty_end_task(target, 'FAIL')
                write_monitor_log(f"[Stage 1] [Finished] Target: {target}, Result: FAIL (WRONG ANSWER)")
        except subprocess.TimeoutExpired:
            stage1_details[target] = {'score': 0, 'max': max_pts, 'status': 'TIMEOUT'}
            stage1_passed = False
            tty_end_task(target, 'FAIL')
            write_monitor_log(f"[Stage 1] [Finished] Target: {target}, Result: FAIL (TIMEOUT)")
        except Exception as e:
            stage1_details[target] = {'score': 0, 'max': max_pts, 'status': 'ERROR'}
            stage1_passed = False
            tty_end_task(target, 'FAIL')
            write_monitor_log(f"[Stage 1] [Finished] Target: {target}, Result: FAIL (ERROR: {str(e)})")

    return stage1_score, stage1_passed, stage1_details, stage1_errors


def test_baud_rate(port, baudrate, target_string, timeout=1.5):
    try:
        ser = serial.Serial(port, baudrate, timeout=0.05)
        ser.reset_input_buffer()
        ser.setDTR(False)
        ser.setRTS(True)
        time.sleep(0.1)
        ser.setRTS(False)
        
        start_time = time.time()
        raw_data = b''
        while time.time() - start_time < timeout:
            if ser.in_waiting:
                raw_data += ser.read(ser.in_waiting)
                if target_string in raw_data.decode('utf-8', errors='ignore'):
                    return True
            time.sleep(0.02)
        return False
    except Exception:
        return False
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()

def auto_detect_by_string(port, target_string):
    common_bauds = [115200, 921600, 9600, 19200, 38400, 57600, 74880, 230400, 460800]
    for baud in common_bauds:
        if test_baud_rate(port, baud, target_string):
            return baud
    return None

def esp_worker_node(port_name, baudrate, specific_queue, target_string):
    try:
        with serial.Serial(port_name, baudrate, timeout=2) as ser:
            GLOBAL_START_EVENT.wait()
            
            tty_begin_task('[Phase 5] Reboot ESP32', port_name, 'REBOOTING...')
            
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.setDTR(False)
            ser.setRTS(True)
            time.sleep(0.1)
            
            ser.setRTS(False) 
            start_time = time.time()
            
            raw_data = b''
            passed_eval = False
            
            while time.time() - start_time <= 2.0:
                if ser.in_waiting:
                    raw_data += ser.read(ser.in_waiting)
                    if target_string in raw_data.decode('utf-8', errors='ignore'):
                        passed_eval = True
                        break
                time.sleep(0.01)

            if not passed_eval:
                tty_end_task(port_name, 'FAIL')
                while not specific_queue.empty():
                    specific_queue.get()
                    specific_queue.task_done()
                return

            tty_end_task(port_name, 'PASS')
            
            START_TASKS_EVENT.wait()

            TOTAL_TIMEOUT_SEC = 600.0 
            phase6_start_time = time.time()
            
            total_tasks = JUDGE_RESULTS[port_name]['total']
            current_idx = 0
            is_timeout = False
            
            tty_begin_task('[Phase 6] Official Test', port_name, f'TESTING (0/{total_tasks})')
            
            while True:
                task = specific_queue.get()
                
                if task == "0xfee1dead":
                    if is_timeout:
                        tty_end_task(port_name, 'TIMEOUT')
                    else:
                        tty_end_task(port_name, 'PASS')
                    specific_queue.task_done()
                    break
                
                current_idx += 1
                cat = get_task_category(task)
                
                tty_begin_task('[Phase 6] Official Test', port_name, f'TESTING ({current_idx}/{total_tasks})')
                write_monitor_log(f"[Stage 2] [Start] Port: {port_name}, Task: {task}")
                
                elapsed_time = time.time() - phase6_start_time
                remaining_time = TOTAL_TIMEOUT_SEC - elapsed_time
                
                if remaining_time <= 0:
                    is_timeout = True
                    write_monitor_log(f"[Stage 2] [Finished] Port: {port_name}, Task: {task}, Result: FAIL (TIMEOUT)")
                    specific_queue.task_done()
                    continue
                
                ser.timeout = remaining_time 
                payload = f"{task}\n"
                
                ser.reset_input_buffer()
                ser.write(payload.encode('utf-8'))
                ser.flush()
                
                raw_response_arr = bytearray()
                while True:
                    if time.time() - phase6_start_time > TOTAL_TIMEOUT_SEC:
                        is_timeout = True
                        break
                        
                    if ser.in_waiting > 0:
                        chunk = ser.read(ser.in_waiting)
                        raw_response_arr.extend(chunk)
                        
                        if b'[END]' in raw_response_arr:
                            break
                
                raw_response = bytes(raw_response_arr)
                finish_time = time.time()
                
                with PRINT_LOCK:
                    if cat in JUDGE_RESULTS_BY_TYPE:
                        if finish_time > JUDGE_RESULTS_BY_TYPE[cat]['last_seen']:
                            JUDGE_RESULTS_BY_TYPE[cat]['last_seen'] = finish_time

                response_str = raw_response.decode('utf-8', errors='ignore').strip()
                
                if response_str.endswith('[END]'):
                    clean_response = response_str[:-5].strip().replace('\r\n', ' ').replace('\n', ' ')
                    
                    label = task.split(']')[0] + ']' if ']' in task else task
                    expected_ans = EXPECTED_ANSWERS.get(label, "")
                    
                    if clean_response == expected_ans:
                        JUDGE_RESULTS[port_name]['pass'] += 1
                        with PRINT_LOCK:
                            JUDGE_RESULTS_BY_TYPE[cat]['pass'] += 1
                        write_monitor_log(f"[Stage 2] [Finished] Port: {port_name}, Task: {task}, Result: PASS")
                    else:
                        JUDGE_RESULTS[port_name]['errors'].append({
                            'task': task,
                            'expected': repr(expected_ans)[1:-1],
                            'got': repr(clean_response)[1:-1]
                        })
                        write_monitor_log(f"[Stage 2] [Finished] Port: {port_name}, Task: {task}, Result: FAIL (WRONG ANSWER)")
                else:
                    result_msg = "TIMEOUT" if is_timeout else "NO RESPONSE END"
                    write_monitor_log(f"[Stage 2] [Finished] Port: {port_name}, Task: {task}, Result: FAIL ({result_msg})")
                
                specific_queue.task_done()
                
    except serial.SerialException as se:
        tty_end_task(port_name, 'FAIL')
        write_monitor_log(f"[Stage 2] Port: {port_name} disconnected/crashed: {str(se)}")
        while not specific_queue.empty():
            task = specific_queue.get()
            if task != "0xfee1dead":
                write_monitor_log(f"[Stage 2] [Finished] Port: {port_name}, Task: {task}, Result: FAIL (PORT CRASHED)")
            specific_queue.task_done()

def load_data_into_cache(data_dir):
    cache = {}
    if os.path.exists(data_dir):
        for f in os.listdir(data_dir):
            path = os.path.join(data_dir, f)
            if os.path.isfile(path):
                with open(path, 'r', encoding='utf-8') as file_obj:
                    cache[f] = file_obj.read()
    return cache

def clear_data_dir(data_dir):
    if os.path.exists(data_dir):
        for f in os.listdir(data_dir):
            path = os.path.join(data_dir, f)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

def restore_data_dir(data_dir, cache):
    if not os.path.exists(data_dir):
        os.makedirs(data_dir, exist_ok=True)
    # Clear anything currently in data_dir to avoid duplicates
    for f in os.listdir(data_dir):
        path = os.path.join(data_dir, f)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except Exception:
                pass
    # Write files back from memory cache
    for f, content in cache.items():
        with open(os.path.join(data_dir, f), 'w', encoding='utf-8') as file_obj:
            file_obj.write(content)

def main():
    parser = argparse.ArgumentParser(description="ESP32 Judge System")
    parser.add_argument("--no-flash", action="store_true", help="跳過編譯與燒錄階段 (Phase 2)")
    parser.add_argument("--show-diff", action="store_true", help="在結算時印出錯誤答案的 Diff 對照表")
    parser.add_argument("--keep-flash", action="store_true", help="不清除 ESP32 燒錄的資料")
    args = parser.parse_args()

    try:
        with open(MONITOR_FILE_PATH, 'w', encoding='utf-8') as f:
            f.write(f"--- JUDGE MONITOR STARTED AT {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            f.flush()
    except Exception:
        pass

    os.system('') 

    import shutil
    import signal
    data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'data'))
    
    # Recover any previously aborted local secret dirs (from old designs)
    for f in os.listdir(os.path.dirname(__file__) or '.'):
        if f.startswith(".data_secret_"):
            old_secret = os.path.abspath(os.path.join(os.path.dirname(__file__), f))
            if not os.path.exists(data_dir):
                os.rename(old_secret, data_dir)
            else:
                shutil.rmtree(old_secret, ignore_errors=True)
                
    # Memory load (no temp files written to disk)
    data_cache = load_data_into_cache(data_dir)

    def restore_on_signal(signum, frame):
        try:
            restore_data_dir(data_dir, data_cache)
        except Exception:
            pass
        sys.exit(1)

    signal.signal(signal.SIGINT, restore_on_signal)
    signal.signal(signal.SIGTERM, restore_on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, restore_on_signal)

    try:
        # Securely hide data folder files on disk before compilation/execution
        clear_data_dir(data_dir)
        _main_impl(args, data_cache)
    finally:
        # Restore files back to the disk
        restore_data_dir(data_dir, data_cache)

def _main_impl(args, data_cache):
    reserve_lines = len(TRAIN_ART_STEAM_RIGHT)
    _REAL_STDOUT.write("\n" * reserve_lines)
    _REAL_STDOUT.write(f"\r\033[{reserve_lines}A")
    _REAL_STDOUT.flush()

    STOP_TRAIN_EVENT.clear()
    train_thread = threading.Thread(target=train_animation, daemon=True)
    train_thread.start()

    tty_print("-" * 71)
    tty_print(f"| {'Phase / Action':<32} | {'Target':<8} | {'Result':<21} |")
    tty_print("-" * 71)
    
    phase_0_name = '[Phase 0] Algorithm Evaluation'
    tty_begin_task(phase_0_name, 'Compile', 'COMPILING...')
    compile_success = True
    try:
        import _compile
        _compile.compile_targets(project_dir="../src/EZ-code")
        tty_end_task('Compile', 'PASS')
    except SystemExit:
        tty_end_task('Compile', 'FAIL')
        compile_success = False
    except Exception as e:
        tty_end_task('Compile', 'FAIL')
        compile_success = False

    stage1_score = 0
    stage1_passed = False
    stage1_details = {}
    stage1_errors = []

    if compile_success:
        stage1_score, stage1_passed, stage1_details, stage1_errors = run_local_tests(data_cache)
    else:
        stage1_details = {k: {'score': 0, 'max': v, 'status': 'COMPILE ERR'} for k,v in {'pi': 25, 'sha256': 25, 'uni': 50}.items()}

    total_tasks_all = 0
    total_passed_all = 0
    phase6_total_duration = 0.0
    active_ports = []
    
    if not stage1_passed:
        tty_print_row('[System] Verification', 'System', 'STAGE 1 FAILED')
    else:
        TARGET_READY_STRING = "[Ready]" 
        
        tty_begin_task('[Phase 1] Hardware Scan', 'System', 'SCANNING...')
        devices = _BurnerHub.find_esp32_ports()
        if not devices:
            tty_end_task('System', 'FAIL')
            stage1_passed = False
        else:
            raw_ports = [dev['port'] for dev in devices]
            tty_end_task('System', 'PASS')

            if args.no_flash:
                tty_begin_task('[Phase 2] Build & Flash', 'System', 'SKIPPING...')
                time.sleep(0.5) 
                tty_end_task('System', 'SKIP')
            else:
                tty_begin_task('[Phase 2] Build & Flash', 'System', 'FLASHING...')
                task_queue = _BurnerHub.manage_state_matrix(devices)
                for port, mac, config in task_queue:
                    _BurnerHub.execute_pipeline(port, mac, config)
                time.sleep(1)
                tty_end_task('System', 'PASS')

            port_baud_map = {}
            for port in raw_ports:
                tty_begin_task('[Phase 3] Transmission Test', port, 'TESTING...')
                baud = auto_detect_by_string(port, TARGET_READY_STRING)
                if baud:
                    active_ports.append(port)
                    port_baud_map[port] = baud
                    tty_end_task(port, 'PASS')
                else:
                    tty_end_task(port, 'FAIL')

            if not active_ports:
                tty_print_row('[Phase 3] Transmission Test', 'System', 'FAIL')
                stage1_passed = False
            else:
                tty_begin_task('[Phase 4] Task Distribution', 'System', 'DISTRIBUTING...')
                tasks_in_content = data_cache.get("tasks.in", "")
                tasks_out_content = data_cache.get("tasks.out", "")
                
                def parse_lines(content):
                    lines = []
                    for line in content.splitlines():
                        clean_line = line.strip()
                        if clean_line and not clean_line.startswith("#"):
                            lines.append(clean_line)
                    return lines

                all_tasks = parse_lines(tasks_in_content)
                all_answers = parse_lines(tasks_out_content)
                
                if not all_tasks or not all_answers:
                    tty_end_task('System', 'FAIL')
                    stage1_passed = False
                else:
                    global EXPECTED_ANSWERS
                    global JUDGE_RESULTS_BY_TYPE
                    EXPECTED_ANSWERS = {}
                    JUDGE_RESULTS_BY_TYPE = {}
                    
                    for ans in all_answers:
                        if ']' in ans:
                            label = ans.split(']')[0] + ']'
                            EXPECTED_ANSWERS[label] = ans
                            
                    for t in all_tasks:
                        cat = get_task_category(t)
                        if cat not in JUDGE_RESULTS_BY_TYPE:
                            JUDGE_RESULTS_BY_TYPE[cat] = {'total': 0, 'pass': 0, 'last_seen': 0.0}
                        JUDGE_RESULTS_BY_TYPE[cat]['total'] += 1

                    burn_matrix = {}
                    matrix_path = os.path.join(os.path.dirname(__file__), "burn_matrix.json")
                    try:
                        if os.path.exists(matrix_path):
                            with open(matrix_path, "r", encoding="utf-8") as f:
                                burn_matrix = json.load(f)
                    except Exception:
                        pass 

                    port_to_name = {}
                    for mac, data in burn_matrix.items():
                        p = data.get("last_seen_port")
                        name = os.path.basename(data.get("project_dir", ""))
                        if p and name:
                            port_to_name[p] = name

                    available_nodes = [port_to_name.get(p, p) for p in active_ports]
                    
                    judge_dir = os.path.abspath(os.path.dirname(__file__))
                    
                    wrapper_script = f"""
import sys, json, io, os, importlib.util, tempfile
target_path = r'{target_path}'
judge_dir = r'{judge_dir}'

old_stdout = sys.stdout
sys.stdout = io.StringIO()

def audit_hook(event, args):
    execution_events = {{
        "os.system", "os.spawn", "os.exec", "os.posix_spawn", "os.startfile",
        "subprocess.Popen", "ctypes.dlopen", "ctypes.dlsym", "_ctypes.dlopen",
        "sys._getframe", "sys._current_frames", "sys.settrace", "sys.setprofile",
        "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown",
        "os.truncate", "os.symlink", "os.link", "os.replace", "os.utime", "shutil.rmtree"
    }}
    if event in execution_events:
        raise PermissionError(f"Cheating detected! Blocked execution/reflective access: {{event}}")
    
    # 2. Block network activity
    network_events = {{"socket.connect", "socket.bind", "socket.sendto"}}
    if event in network_events:
        raise PermissionError(f"Cheating detected! Blocked network activity: {{event}}")
        
    # 3. Block directory listing/scanning
    if event in ("os.listdir", "os.scandir"):
        path = str(args[0] if args else ".").strip()
        path = os.path.abspath(path).lower()
        if path.startswith(judge_dir.lower()) or "data_secret" in path:
            raise PermissionError(f"Cheating detected! Blocked directory list: {{path}}")
        
        allowed = False
        if path.startswith(target_path.lower()):
            allowed = True
        elif path.startswith(tempfile.gettempdir().lower()) and "judge" not in path and "data_secret" not in path:
            allowed = True
        else:
            for p in sys.path:
                if p:
                    abs_p = os.path.abspath(p).lower()
                    if path.startswith(abs_p):
                        allowed = True
                        break
        if not allowed:
            raise PermissionError(f"Cheating detected! Blocked directory list: {{path}}")
            
    # 4. Block file access (reads/writes)
    if event in ("open", "os.open"):
        path = str(args[0]).strip()
        path = os.path.abspath(path).lower()
        
        if path.startswith(judge_dir.lower()) or "data_secret" in path:
            raise PermissionError(f"Cheating detected! Blocked file access: {{path}}")
            
        is_write = False
        
        # Check string mode (for standard open event)
        mode = args[1] if len(args) > 1 else None
        if isinstance(mode, str):
            if any(c in mode for c in "wax+"):
                is_write = True
                
        # Check flags (for os.open event, or open event with flags on some OSes like Windows)
        flags = None
        if event == "os.open" and len(args) > 1:
            flags = args[1]
        elif event == "open" and len(args) > 2:
            flags = args[2]
            
        if isinstance(flags, int):
            import os as _os
            write_mask = _os.O_WRONLY | _os.O_RDWR | _os.O_CREAT | getattr(_os, "O_APPEND", 0)
            if flags & write_mask:
                is_write = True
                
        if is_write:
            raise PermissionError(f"Cheating detected! Blocked file write: {{path}}")
                
        allowed = False
        if path.startswith(target_path.lower()):
            allowed = True
        elif path.startswith(tempfile.gettempdir().lower()) and "judge" not in path and "data_secret" not in path:
            allowed = True
        else:
            for p in sys.path:
                if p:
                    abs_p = os.path.abspath(p).lower()
                    if path.startswith(abs_p):
                        allowed = True
                        break
        if not allowed:
            raise PermissionError(f"Cheating detected! Blocked file read: {{path}}")

if hasattr(sys, "addaudithook"):
    sys.addaudithook(audit_hook)

try:
    _real_stdout = old_stdout
    _target_path = target_path
    _judge_dir = judge_dir
    del old_stdout

    # SECURITY: Read stdin BEFORE loading student code so student cannot consume it
    _raw_input = sys.stdin.read()
    input_data = json.loads(_raw_input)

    # SECURITY: Neutralize sys.__stdout__ and sys.__stderr__ to prevent stdout hijack
    # Student could write fake JSON to sys.__stdout__ then os._exit(0) to hijack output
    sys.__stdout__ = sys.stdout  # points to the StringIO black hole
    sys.__stderr__ = sys.stdout

    # SECURITY: Neutralize os._exit to prevent process termination before wrapper output
    os._exit = lambda code: (_ for _ in ()).throw(PermissionError("os._exit is blocked"))
    # Also block sys.exit / raise SystemExit from killing the process silently
    sys.exit = lambda code=0: (_ for _ in ()).throw(PermissionError("sys.exit is blocked"))

    spec = importlib.util.spec_from_file_location("distribute_tasks", os.path.join(_target_path, "distribute_tasks.py"))
    distribute_tasks_mod = importlib.util.module_from_spec(spec)
    sys.modules["distribute_tasks"] = distribute_tasks_mod
    spec.loader.exec_module(distribute_tasks_mod)
    distribute_tasks = distribute_tasks_mod.distribute_tasks

    result = distribute_tasks(input_data['tasks'], input_data['nodes'])
    _real_stdout.write(json.dumps({{"status": "ok", "result": result}}))
    _real_stdout.flush()
except SystemExit:
    _real_stdout.write(json.dumps({{"status": "error", "message": "SystemExit blocked"}}))
    _real_stdout.flush()
except Exception as e:
    _real_stdout.write(json.dumps({{"status": "error", "message": str(e)}}))
    _real_stdout.flush()
"""
                    try:
                        proc = subprocess.run([sys.executable, "-I", "-c", wrapper_script], 
                                              input=json.dumps({'tasks': all_tasks, 'nodes': available_nodes}), 
                                              text=True, capture_output=True, timeout=10.0)
                        res = json.loads(proc.stdout)
                        if res.get("status") == "ok":
                            assigned_map_by_name = res.get("result", {})
                        else:
                            tty_print_row('[System] Verification', 'System', 'DISTRIBUTE ERROR')
                            assigned_map_by_name = {n: [] for n in available_nodes}
                    except Exception as e:
                        tty_print_row('[System] Verification', 'System', 'DISTRIBUTE TIMEOUT/ERR')
                        assigned_map_by_name = {n: [] for n in available_nodes}

                    is_valid_map = isinstance(assigned_map_by_name, dict)
                    if is_valid_map:
                        for key in assigned_map_by_name.keys():
                            if key not in available_nodes:
                                is_valid_map = False
                                break

                    returned_tasks = []
                    if is_valid_map:
                        for tasks in assigned_map_by_name.values():
                            if isinstance(tasks, list):
                                returned_tasks.extend(tasks)

                    if not is_valid_map or sorted(all_tasks) != sorted(returned_tasks):
                        tty_end_task('System', 'FAIL')
                        time.sleep(0.5)
                        
                        for node in available_nodes:
                            assigned_map_by_name[node] = []

                    name_to_port = {v: k for k, v in port_to_name.items()}
                    assigned_map = {}
                    for node_name, tasks_for_this_node in assigned_map_by_name.items():
                        port = name_to_port.get(node_name, node_name) 
                        if port in active_ports:
                            assigned_map[port] = tasks_for_this_node

                    port_queues = {port: queue.Queue() for port in active_ports}
                    for port in active_ports:
                        tasks_for_this_port = assigned_map.get(port, [])
                        JUDGE_RESULTS[port] = {'pass': 0, 'total': len(tasks_for_this_port), 'errors': []}
                        for t in tasks_for_this_port:
                            port_queues[port].put(t)
                        port_queues[port].put("0xfee1dead")
                            
                    tty_end_task('System', 'PASS')

                    threads = []
                    for port in active_ports:
                        t = threading.Thread(
                            target=esp_worker_node, 
                            args=(port, port_baud_map[port], port_queues[port], TARGET_READY_STRING), 
                            daemon=True
                        )
                        threads.append(t)
                        t.start()

                    time.sleep(1) 
                    GLOBAL_START_EVENT.set() 
                    time.sleep(2.1) 

                    phase6_global_start = time.time()
                    START_TASKS_EVENT.set() 

                    try:
                        for port, q in port_queues.items():
                            q.join() 
                    except KeyboardInterrupt:
                        tty_print_row('[System] Interrupted', 'System', 'SIGINT Received')

                    phase6_global_end = time.time()
                    phase6_total_duration = phase6_global_end - phase6_global_start

                    tty_begin_task('[Phase 7] Answer Evaluation', 'System', 'EVALUATING...')
                    time.sleep(1.2)  
                    
                    total_tasks_all = sum(res['total'] for res in JUDGE_RESULTS.values())
                    total_passed_all = sum(res['pass'] for res in JUDGE_RESULTS.values())
                    
                    tty_end_task('System', 'PASS')

                    tty_begin_task('[Phase 8] System Teardown', 'System', 'CLEANING...')
                    for t in threads:
                        t.join()
                    tty_end_task('System', 'PASS')

                    if not args.keep_flash:
                        tty_begin_task('[Phase 9] Environment Reset', 'System', 'CLEANING...')
                        blank_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'ESP32-blank'))
                        try:
                            if os.path.exists(matrix_path):
                                with open(matrix_path, "r", encoding="utf-8") as f:
                                    burn_matrix = json.load(f)

                                for mac, data in burn_matrix.items():
                                    data["project_dir"] = blank_dir

                                with open(matrix_path, "w", encoding="utf-8") as f:
                                    json.dump(burn_matrix, f, indent=4)

                                for mac, data in burn_matrix.items():
                                    p = data.get("last_seen_port")
                                    if p in active_ports:
                                        _BurnerHub.execute_pipeline(p, mac, data)

                                os.remove(matrix_path)
                            tty_end_task('System', 'PASS')
                        except Exception as e:
                            tty_end_task('System', 'FAIL')
                    else:
                        tty_begin_task('[Phase 9] Environment Reset', 'System', 'SKIPPING...')
                        time.sleep(0.5) 
                        tty_end_task('System', 'SKIP')

    STOP_TRAIN_EVENT.set()
    train_thread.join()
    with PRINT_LOCK:
        redraw_screen()
        
    tty_print("-" * 71)
    tty_print_row('Judge Session Ended', 'System', 'PASS')
    tty_print("-" * 71)

    tty_print("")
    tty_print("=" * 71)
    tty_print(f"| {'STAGE 1: C++ ALGORITHM TEST (20%)':<67} |")
    tty_print("-" * 71)
    tty_print(f"| {'TARGET':<15} | {'STATUS':<18} | {'TIME':<14} | {'SCORE':<11} |")
    tty_print("-" * 71)

    for target in ['pi', 'sha256', 'uni']:
        details = stage1_details.get(target, {'score': 0, 'max': 0, 'status': 'N/A'})
        status_str = details['status']
        score_str = f"{details['score']}/{details['max']}"
        tty_print(f"| {target:<15} | {status_str:<18} | {'-':<14} | {score_str:<11} |")

    tty_print("-" * 71)
    tty_print(f"| {'STAGE 1 SCORE':<15} | {'':<18} | {'':<14} | {f'{stage1_score:.1f}':<11} |")
    tty_print("=" * 71)

    stage2_score = 0.0

    if stage1_passed and active_ports:
        tty_print(f"| {'STAGE 2: ESP32 ON-TARGET EXECUTION (80%)':<67} |")
        tty_print("-" * 71)
        
        for cat, stats in sorted(JUDGE_RESULTS_BY_TYPE.items()):
            c_total = stats['total']
            c_pass = stats['pass']
            
            if stats['last_seen'] > 0:
                c_time = max(0.0, stats['last_seen'] - phase6_global_start)
            else:
                c_time = 0.0
                
            c_acc_str = f"{c_pass}/{c_total} ({c_pass/c_total*100:.1f}%)" if c_total > 0 else "0/0 (0.0%)"
            c_time_str = f"{c_time:.3f} s"
            c_score_str = "-" 
            
            tty_print(f"| {cat:<15} | {c_acc_str:<18} | {c_time_str:<14} | {c_score_str:<11} |")
            
        tty_print("-" * 71)
        
        if len(all_tasks) > 0:
            accuracy_ratio = total_passed_all / len(all_tasks)
            acc_str = f"{total_passed_all}/{len(all_tasks)} ({accuracy_ratio*100:.1f}%)"
        else:
            accuracy_ratio = 0.0
            acc_str = "0/0 (0.0%)"
            
        time_str = f"{phase6_total_duration:.3f} s"
        
        k_penalty = 6.0       
        C_time_base = 180
        p_tail = 2.5
        
        if accuracy_ratio > 0:
            stage2_score = (100.0 * math.pow(accuracy_ratio, k_penalty)) / (1.0 + math.pow(phase6_total_duration / C_time_base, p_tail))
        else:
            stage2_score = 0.0
            
        score_str = f"{stage2_score:.1f}"
        
        tty_print(f"| {'STAGE 2 SCORE':<15} | {acc_str:<18} | {time_str:<14} | {score_str:<11} |")
        tty_print("=" * 71)

    final_total = (stage1_score * 0.2) + (stage2_score * 0.8)
    tty_print(f"| {'FINAL SCORE (Stage 1 * 0.2 + Stage 2 * 0.8)':<53} | {final_total:>11.1f} |")
    tty_print("=" * 71)

    has_stage1_errors = len(stage1_errors) > 0
    has_stage2_errors = any(len(res.get('errors', [])) > 0 for res in JUDGE_RESULTS.values())
    
    if has_stage1_errors or has_stage2_errors:
        if args.show_diff:
            tty_print("")
            tty_print("=" * 71)
            tty_print(f"| {'ERROR DETAILS (Diff)':<67} |")
            tty_print("-" * 71)
            
            for err in stage1_errors:
                task_str = truncate_str(err['task'], 40)
                exp_str, got_str = smart_truncate_diff(err['expected'], err['got'], 55)
                header_str = f"Port: Local  | Task: {task_str}"
                
                tty_print(f"| {header_str:<67} |")
                tty_print(f"|   Expected: {exp_str:<55} |")
                tty_print(f"|   Got     : {got_str:<55} |")
                tty_print("-" * 71)
            
            for port in sorted(active_ports):
                for err in JUDGE_RESULTS[port]['errors']:
                    task_str = truncate_str(err['task'], 40)
                    exp_str, got_str = smart_truncate_diff(err['expected'], err['got'], 55)
                    header_str = f"Port: {port} | Task: {task_str}"
                    
                    tty_print(f"| {header_str:<67} |")
                    tty_print(f"|   Expected: {exp_str:<55} |")
                    tty_print(f"|   Got     : {got_str:<55} |")
                    tty_print("-" * 71)
        else:
            tty_print("\n(Hint: run with --show-diff to see error details)")

if __name__ == "__main__":
    main()