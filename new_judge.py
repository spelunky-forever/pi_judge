import serial
import time
import threading
import queue
import sys
import os
import argparse
import json  
import math  
import unicodedata
import re
import random
import shutil
import signal

import _BurnerHub

_REAL_STDOUT = sys.__stdout__
sys.stdout = open(os.devnull, 'w')

PRINT_LOCK = threading.Lock()
GLOBAL_START_EVENT = threading.Event()  
START_TASKS_EVENT = threading.Event()   
STOP_TRAIN_EVENT = threading.Event()    

PERMANENT_LOGS = queue.Queue()  
CURRENT_DYNAMIC_TASK = None      
LAST_DYNAMIC_LINES = 0          

JUDGE_RESULTS = {}              
JUDGE_RESULTS_BY_TYPE = {}      
EXPECTED_ANSWERS = {}           

MONITOR_FILE_LOCK = threading.Lock()
MONITOR_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'monitor_status.txt'))

PI_CASES = [
    ("pi1", 50.0),
    ("pi2", 180.0),
    ("pi3", 180.0)
]

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

def new_redraw_screen():
    global LAST_DYNAMIC_LINES, CURRENT_DYNAMIC_TASK
    
    if LAST_DYNAMIC_LINES > 1:
        _REAL_STDOUT.write(f"\r\033[{LAST_DYNAMIC_LINES - 1}A")
    elif LAST_DYNAMIC_LINES == 1:
        _REAL_STDOUT.write("\r")
        
    while not PERMANENT_LOGS.empty():
        _REAL_STDOUT.write(f"\033[2K\r{PERMANENT_LOGS.get()}\n")
        
    dynamic_output = []
    
    if CURRENT_DYNAMIC_TASK is not None:
        desc = CURRENT_DYNAMIC_TASK['description']
        level = CURRENT_DYNAMIC_TASK.get('level', 'System')
        status = CURRENT_DYNAMIC_TASK['status']
        dynamic_output.append(f"| {CJK(desc):<31} | {CJK(level):<12} | {CJK(status):<18} |")
        
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
            new_redraw_screen()
        time.sleep(0.08)

def tty_print(message):
    with PRINT_LOCK:
        PERMANENT_LOGS.put(message)
        new_redraw_screen()

def tty_new_dynamic_print(description, level, status):
    global CURRENT_DYNAMIC_TASK
    with PRINT_LOCK:
        CURRENT_DYNAMIC_TASK = {
            "description": description,
            "level": level,
            "status": status
        }
        new_redraw_screen()

class CJK:
    """Aligns CJK characters correctly in f-strings."""
    def __init__(self, text):
        self.text = str(text)
        
    def __format__(self, format_spec):
        if not format_spec:
            return self.text
            
        match = re.match(r'^(?P<prefix>.*?)(?P<width>\d+)$', format_spec)
        
        if not match:
            return format(self.text, format_spec) 
            
        prefix = match.group('prefix')
        total_width = int(match.group('width'))
        
        if len(prefix) == 0:
            fillchar, align = ' ', '<'              
        elif len(prefix) == 1:
            if prefix in '<>=^':
                fillchar, align = ' ', prefix       
            else:
                return format(self.text, format_spec)
        elif len(prefix) == 2:
            fillchar, align = prefix[0], prefix[1]  
            if align not in '<>=^':
                return format(self.text, format_spec)
        else:
            return format(self.text, format_spec)
        
        visual_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in self.text)
        pad_len = total_width - visual_width
        
        if pad_len <= 0:
            return self.text
            
        if align == '<':
            return self.text + (fillchar * pad_len)
        elif align == '>':
            return (fillchar * pad_len) + self.text
        elif align == '^':
            left = pad_len // 2
            right = pad_len - left
            return (fillchar * left) + self.text + (fillchar * right)
            
        return self.text

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

def esp_worker_node(port_name, baudrate, specific_queue, target_string, case_timeout_sec, case_name):
    try:
        level_str = case_name.upper().replace('PI', 'LEVEL ')
        with serial.Serial(port_name, baudrate, timeout=2) as ser:
            tty_new_dynamic_print(f"🔌 喚醒賴床的卡皮巴拉~", level_str, "⚡ 暖機 (REBOOT)")
            GLOBAL_START_EVENT.wait()

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
                while not specific_queue.empty():
                    specific_queue.get()
                    specific_queue.task_done()
                return

            START_TASKS_EVENT.wait()

            TOTAL_TIMEOUT_SEC = case_timeout_sec
            phase6_start_time = time.time()
            total_tasks = JUDGE_RESULTS[case_name]['total']
            current_idx = 0
            is_timeout = False

            tty_new_dynamic_print(f"🍡 準備餵食...", level_str, f"🍽️ 等待中: 0/{total_tasks}")

            while True:
                task = specific_queue.get()

                if task == "0xfee1dead":
                    JUDGE_RESULTS[case_name]['is_timeout'] = is_timeout
                    
                    global CURRENT_DYNAMIC_TASK
                    CURRENT_DYNAMIC_TASK = None
                    phase_text = f"🍡 餵食結算:"
                    progress_text = f"🍽️ {current_idx}/{total_tasks}份"
                    if is_timeout:
                        result_text = "💤 睡著 TIMEOUT"
                    else:
                        result_text = "⏰ 進食結束"

                    tty_print(f"| {CJK(phase_text):<31} | {CJK(progress_text):<12} | {CJK(result_text):<18} |")
                    specific_queue.task_done()
                    break

                current_idx += 1
                cat = get_task_category(task)

                label = task.split(']')[0] + ']' if ']' in task else task
                expected_ans = EXPECTED_ANSWERS.get(label, "")

                food_menu = ['🥬', '🥕', '🍉', '🍠', '🍎']
                current_food = food_menu[current_idx % len(food_menu)]
                
                desc_text = f"🍡 餵食中..."
                status_text = f"{current_food} 嚼嚼: {current_idx}/{total_tasks}"
                tty_new_dynamic_print(desc_text, level_str, status_text)

                write_monitor_log(f"[Stage 2] [Start] Port: {port_name}, Task: {task}")

                elapsed_time = time.time() - phase6_start_time
                remaining_time = TOTAL_TIMEOUT_SEC - elapsed_time

                if remaining_time <= 0:
                    is_timeout = True
                    write_monitor_log(f"[Stage 2] [Finished] Case: {case_name}, Task: {task}, Result: FAIL (TIMEOUT)")
                    JUDGE_RESULTS[case_name]['errors'].append({
                        'task': task,
                        'expected': repr(expected_ans)[1:-1],
                        'got': "💤 卡皮巴拉等到睡著了 (TIMEOUT)"
                    })
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

                    if clean_response == expected_ans:
                        JUDGE_RESULTS[case_name]['pass'] += 1
                        with PRINT_LOCK:
                            JUDGE_RESULTS_BY_TYPE[cat]['pass'] += 1
                        write_monitor_log(f"[Stage 2] [Finished] Case: {case_name}, Task: {task}, Result: PASS")
                    else:
                        JUDGE_RESULTS[case_name]['errors'].append({
                            'task': task,
                            'expected': repr(expected_ans)[1:-1],
                            'got': repr(clean_response)[1:-1]
                        })
                        write_monitor_log(f"[Stage 2] [Finished] Case: {case_name}, Task: {task}, Result: FAIL (WRONG ANSWER)")
                else:
                    result_msg = "TIMEOUT" if is_timeout else "NO RESPONSE END"
                    JUDGE_RESULTS[case_name]['errors'].append({
                        'task': task,
                        'expected': repr(expected_ans)[1:-1],
                        'got': f"🚫 卡皮巴拉拒食 ({result_msg})"
                    })
                    write_monitor_log(f"[Stage 2] [Finished] Case: {case_name}, Task: {task}, Result: FAIL ({result_msg})")

                specific_queue.task_done()

    except serial.SerialException as se:
        write_monitor_log(f"[Stage 2] Port: {port_name} disconnected/crashed: {str(se)}")
        while not specific_queue.empty():
            task = specific_queue.get()
            if task != "0xfee1dead":
                write_monitor_log(f"[Stage 2] [Finished] Case: {case_name}, Task: {task}, Result: FAIL (PORT CRASHED)")
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
    for f in os.listdir(data_dir):
        path = os.path.join(data_dir, f)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except Exception:
                pass
    for f, content in cache.items():
        with open(os.path.join(data_dir, f), 'w', encoding='utf-8') as file_obj:
            file_obj.write(content)

def main():
    parser = argparse.ArgumentParser(description="ESP32 Judge System")
    parser.add_argument("--show-diff", action="store_true", help="在結算時印出錯誤答案的 Diff 對照表")
    args = parser.parse_args()

    try:
        with open(MONITOR_FILE_PATH, 'w', encoding='utf-8') as f:
            f.write(f"--- JUDGE MONITOR STARTED AT {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            f.flush()
    except Exception:
        pass

    os.system('') 

    data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'data'))
    
    for f in os.listdir(os.path.dirname(__file__) or '.'):
        if f.startswith(".data_secret_"):
            old_secret = os.path.abspath(os.path.join(os.path.dirname(__file__), f))
            if not os.path.exists(data_dir):
                os.rename(old_secret, data_dir)
            else:
                shutil.rmtree(old_secret, ignore_errors=True)
                
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
        clear_data_dir(data_dir)
        _main_impl(args, data_cache)
    finally:
        restore_data_dir(data_dir, data_cache)

def _main_impl(args, data_cache):
    time_base3, time_err = 11.5, 5.0
    reserve_lines = len(TRAIN_ART_STEAM_RIGHT)
    _REAL_STDOUT.write("\n" * reserve_lines)
    _REAL_STDOUT.write(f"\r\033[{reserve_lines}A")
    _REAL_STDOUT.flush()

    STOP_TRAIN_EVENT.clear()
    train_thread = threading.Thread(target=train_animation, daemon=True)
    train_thread.start()

    total_tasks_all = 0
    total_passed_all = 0
    phase6_total_duration = 0.0
    active_ports = []
    phase6_global_start = time.time()

    TARGET_READY_STRING = "[Ready]"
    ending = 0 

    try:
        tty_print(f"+{'':=^69}+")
        tty_print(f"|{CJK(' 🎉 歡迎來到卡皮巴拉餵食秀！ 🎉 '):^69}|")
        tty_print(f"|{'':-^69}|")
        tty_print(f"|{CJK('( ´ ▽ ` )ﾉ 即將開始餵食，確認基本環境中...'):^69}|")
        tty_print(f"+{'':=^69}+")
        devices = _BurnerHub.find_esp32_ports()
        
        if not devices:
            err_title = "🔍 尋找卡皮巴拉: 我的 ESP32 呢？！"
            err_msg = "⚠️ 警告: No ESP32 Detected"
            tty_print(f"| {CJK(err_title):<32} | {CJK(err_msg):<31}|")
            tty_print(f"+{'':=^69}+")
            ending = 1
        else:
            ready_devices = _BurnerHub.manage_state_matrix(devices)

            if not ready_devices:
                ending = 2
            else:
                port, mac, config = ready_devices[0]
                active_ports = [port]

            def parse_lines(content):
                return [
                    line.strip()
                    for line in content.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]

            global EXPECTED_ANSWERS
            global JUDGE_RESULTS_BY_TYPE
            global JUDGE_RESULTS

            EXPECTED_ANSWERS = {}
            JUDGE_RESULTS_BY_TYPE = {}
            JUDGE_RESULTS = {}

            execution_times = {}
            total_tasks_all = 0
            total_passed_all = 0
            phase6_total_duration = 0.0
            phase6_global_start = time.time()

            for idx, (pi_name, pi_timeout) in enumerate(PI_CASES):
                level_str = pi_name.upper().replace('PI', 'LEVEL ')
                target_proj_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'implementation', pi_name))
                config['project_dir'] = target_proj_dir
                
                tty_new_dynamic_print(f"🥘 烤蛋糕中~", level_str, "FLASHING...")
                
                try:
                    _BurnerHub.execute_pipeline(port, mac, config)
                    time.sleep(1)
                    
                    CURRENT_DYNAMIC_TASK = None
                    tty_print(f"| {CJK(f'🍳 蛋糕美味出爐~'):<31} | {CJK(level_str):<12} | {CJK('🌟 PASS 🌟'):<18} |")
                    
                except Exception:
                    CURRENT_DYNAMIC_TASK = None
                    tty_print(f"| {CJK(f'❌ 蛋糕不小心烤焦~'):<31} | {CJK(level_str):<12} | {CJK('💔 FAIL 💔'):<18} |")
                    tty_print(f"| {CJK(f'🚨 廚房突然炸掉~'):<31} | {CJK(level_str):<12} | {CJK('BUILD/FLASH FAILED'):<18} |")
                    ending = 2
                    continue
            
                tty_new_dynamic_print(f"📡 呼叫卡皮巴拉~", level_str, "SYNCING...")
                baud = auto_detect_by_string(port, TARGET_READY_STRING)
                CURRENT_DYNAMIC_TASK = None
                
                if not baud:
                    tty_print(f"| {CJK(f'❌ 卡皮巴拉沒聽到: {pi_name}'):<31} | {CJK(level_str):<12} | {CJK('💔 FAIL 💔'):<18} |")
                    tty_print(f"| {CJK(f'💤 睡得太沉叫不醒 ({pi_name})'):<31} | {CJK(level_str):<12} | {CJK('BOARD NOT READY'):<18} |")
                    ending = 1
                    continue
                
                tasks_in_content = data_cache.get(f"{pi_name}.in", "")
                tasks_out_content = data_cache.get(f"{pi_name}.out", "")

                all_tasks = parse_lines(tasks_in_content)
                all_answers = parse_lines(tasks_out_content)

                if not all_tasks or not all_answers:
                    tty_new_dynamic_print('[Phase 4] Missing Case File', level_str, 'FAIL')
                    continue

                EXPECTED_ANSWERS = {}
                for ans in all_answers:
                    if ']' in ans:
                        label = ans.split(']')[0] + ']'
                        EXPECTED_ANSWERS[label] = ans

                for t in all_tasks:
                    cat = get_task_category(t)
                    if cat not in JUDGE_RESULTS_BY_TYPE:
                        JUDGE_RESULTS_BY_TYPE[cat] = {'total': 0, 'pass': 0, 'last_seen': 0.0}
                    JUDGE_RESULTS_BY_TYPE[cat]['total'] += 1

                port_queue = queue.Queue()
                for t in all_tasks:
                    port_queue.put(t)
                port_queue.put("0xfee1dead")

                JUDGE_RESULTS[pi_name] = {'pass': 0, 'total': len(all_tasks), 'errors': []}

                GLOBAL_START_EVENT.clear()
                START_TASKS_EVENT.clear()

                worker = threading.Thread(
                    target=esp_worker_node,
                    args=(port, baud, port_queue, TARGET_READY_STRING, pi_timeout, pi_name),
                    daemon=True
                )
                worker.start()

                time.sleep(1)
                GLOBAL_START_EVENT.set()
                time.sleep(2.1)

                case_start_time = time.time()
                START_TASKS_EVENT.set()

                port_queue.join()
                execution_times[pi_name] = time.time() - case_start_time
                phase6_total_duration += time.time() - case_start_time

                total_tasks_all += len(all_tasks)
                total_passed_all += JUDGE_RESULTS[pi_name]['pass']

                worker.join()

                if JUDGE_RESULTS[pi_name].get('is_timeout', False):
                    ending = 3
                elif JUDGE_RESULTS[pi_name]['pass'] < JUDGE_RESULTS[pi_name]['total']:
                    ending = 4

                blank_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'ESP32-blank'))
                matrix_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "burn_matrix.json"))
                try:
                    if os.path.exists(matrix_path):
                        with open(matrix_path, "r", encoding="utf-8") as f:
                            burn_matrix = json.load(f)

                        for mac_key, data in burn_matrix.items():
                            data["project_dir"] = blank_dir

                        with open(matrix_path, "w", encoding="utf-8") as f:
                            json.dump(burn_matrix, f, indent=4)

                        for mac_key, data in burn_matrix.items():
                            p = data.get("last_seen_port")
                            if p in active_ports:
                                _BurnerHub.execute_pipeline(p, mac_key, data)

                        os.remove(matrix_path)
                except Exception:
                    pass

                if idx < len(PI_CASES) - 1:
                    tty_print(f"+{'':-^69}+")
                    tty_print(f"|{CJK(' 🍵 中場休息，準備端上下一盤... 🍵 '):-^69}|")
                    tty_print(f"+{'':-^69}+")

    finally:
        STOP_TRAIN_EVENT.set()
        train_thread.join()
        with PRINT_LOCK:
            new_redraw_screen()

    if(ending == 0):
        tty_print(f"+{'':=^69}+")
        tty_print(f"|{CJK(' ✨ 餵食秀圓滿落幕！卡皮巴拉拍了拍肚皮 (๑´ڡ`๑) ✨ '):^69}|")
        tty_print(f"+{'':=^69}+")
    elif(ending == 1):
        tty_print(f"+{'':=^69}+")
        tty_print(f"|{CJK(' 🍃 找不到卡皮巴拉！飼料撒了一地...( ºΔº ) 🍃 '):^69}|")
        tty_print(f"+{'':=^69}+")
    elif(ending == 2):
        tty_print(f"+{'':=^69}+")
        tty_print(f"|{CJK(' 💥 碰！遇上了未知事故，卡皮巴拉嚇到躲起來了 (つд⊂) 💥 '):^69}|")
        tty_print(f"+{'':=^69}+")
    elif(ending == 3):
        tty_print(f"+{'':=^69}+")
        tty_print(f"|{CJK(' 🥣 卡皮巴拉一臉嫌棄，一口都不肯吃... ( ´•̥×•̥` ) 🥣 '):^69}|")
        tty_print(f"+{'':=^69}+")
    elif(ending == 4):
        tty_print(f"+{'':=^69}+")
        tty_print(f"|{CJK(' 🍰 卡皮巴拉挑食中，只吃了一部分蛋糕 ( ˘•ω•˘ ) 🍰 '):^69}|")
        tty_print(f"+{'':=^69}+")

    tty_print("")
    tty_print("=" * 71)
    tty_print(f"| {CJK('📊 餵食總結算 (卡皮巴拉進食報告)'):<67} |")
    tty_print("-" * 71)
    tty_print(f"| {'LEVEL':<15} | {'STATUS':<18} | {'TIME':<14} | {'SCORE':<11} |")
    tty_print("-" * 71)

    if total_tasks_all > 0:
        acc_str = f"{total_passed_all}/{total_tasks_all} ({(total_passed_all / total_tasks_all) * 100:.1f}%)"
    else:
        acc_str = "0/0 (0.0%)"
    time_str = f"{phase6_total_duration:.3f} s"
    level_score = 0.0
    total_score = 0.0

    for pi_name, stats in sorted(JUDGE_RESULTS.items()):
        c_total = stats['total']
        c_pass = stats['pass']
        
        c_time = execution_times.get(pi_name, 0.0) 
        
        c_acc_str = f"{c_pass}/{c_total} ({c_pass/c_total*100:.1f}%)" if c_total > 0 else "0/0 (0.0%)"
        c_time_str = f"{c_time:.3f} s"
        
        level_str = pi_name.upper().replace('PI', 'LEVEL ')

        if(pi_name=='pi1' or pi_name=='pi2'):
            level_score = (c_pass / c_total) * 30 if c_total > 0 else 0.0
        elif(pi_name=='pi3'):
            level_score = (c_pass / c_total) * 40 if c_total > 0 else 0.0
            effective_time = c_time - time_err if c_time > time_err else 0.0
            time_ratio = time_base3 / effective_time if effective_time > time_base3 else 1.0
            level_score *= time_ratio

        total_score += level_score
        
        tty_print(f"| {level_str:<15} | {c_acc_str:<18} | {c_time_str:<14} | {f'{level_score:.1f}':<11} |")

    tty_print("-" * 71)
    tty_print(f"| {'STAGE TOTAL':<15} | {acc_str:<18} | {time_str:<14} | {f'{total_score:.1f}':<11} |")
    tty_print("=" * 71)

    final_total = total_score
    tty_print(f"| {CJK('🌟 綜合飽足指數 (FINAL SCORE)'):<53} | {final_total:>11.1f} |")
    tty_print("=" * 71)

    if final_total == 100.0:
        title = "🏆 稱號：【傳說中的米其林三星飼養員】"
        comments = [
            "太神啦！卡皮巴拉把盤子舔得閃閃發光！",
            "教科書級的完美飼料！感動到流下眼淚。",
            "零 Bug 極致美味！牠決定跟你一輩子了。",
            "神級正確率！卡皮巴拉爽到原地升級啦！",
            "滿分！今年的諾貝爾獎就頒給這段 Code！"
        ]
    elif final_total >= 60.0:
        title = "👨‍🍳 稱號：【卡皮巴拉專屬特級廚師】"
        comments = [
            "不錯唷！卡皮巴拉給了你一個讚賞的眼神。",
            "這飼料超越了學餐水準！卡皮巴拉表示滿意。",
            "味道及格！足以讓卡皮巴拉快樂地泡溫泉了。",
            "咀嚼飛快！看來你的演算法跟排程沒白學！",
            "打個飽嗝，並把這菜加進 Steam 願望清單！"
        ]
    elif final_total > 0.0:
        title = "🌪️ 稱號：【黑暗料理界新星】"
        comments = [
            "吃了一口眉頭一皺，發現案情並不單純...",
            "當減肥餐吧？卡皮巴拉勉強吃幾口就去玩沙了。",
            "這味道...就像修 Bug 到凌晨三點的心酸。",
            "這飼料有期中考被當的無力感...嚼不動啊！",
            "吃了一點點，剩下的被偷偷丟進資源回收桶！"
        ]
    else:
        title = "☠️ 稱號：【地獄廚房學徒】"
        comments = [
            "卡皮巴拉寧願啃杜邦線，也不碰這程式碼！",
            "這是生化武器？卡皮巴拉的血壓都比這分數高！",
            "連壓倒性負評的爛 Game 都比這好啃！已退款。",
            "飼料引發 Seg Fault，卡皮巴拉直接當機啦！",
            "零分！這報廢率簡直比期末考微積分還慘烈！"
        ]

    chosen_comment = random.choice(comments)
    tty_print(f"| {CJK(title):<67} |")
    tty_print(f"| {CJK('💬 評語：' + chosen_comment):<67} |")
    tty_print("=" * 71)

    has_stage2_errors = any(len(res.get('errors', [])) > 0 for res in JUDGE_RESULTS.values())

    if has_stage2_errors:
        if args.show_diff:
            tty_print("")
            tty_print("=" * 71)
            tty_print(f"| {'ERROR DETAILS (Diff)':<67} |")
            tty_print("-" * 71)
            for case_name in sorted(JUDGE_RESULTS.keys()):
                for err in JUDGE_RESULTS[case_name]['errors']:
                    task_str = truncate_str(err['task'], 40)
                    exp_str, got_str = smart_truncate_diff(err['expected'], err['got'], 55)
                    header_str = f"Case: {case_name} | Task: {task_str}"
                    tty_print(f"| {header_str:<67} |")
                    tty_print(f"|   Expected: {exp_str:<55} |")
                    tty_print(f"|   Got     : {got_str:<55} |")
                    tty_print("-" * 71)
        else:
            tty_print("\n(Hint: run with --show-diff to see error details)")


if __name__ == "__main__":
    main()