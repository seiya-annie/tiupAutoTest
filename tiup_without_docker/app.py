import os
import subprocess
import random
import time
import json
import threading
from uuid import uuid4
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from packaging.version import Version
import mysql.connector
import sys

# --- 配置 ---
app = Flask(__name__)
# 用于 session 加密，请在生产环境中替换为更复杂的密钥
app.secret_key = 'a_very_secret_key_for_tidb_tester_tiup'

# 用于存储后台任务的状态和结果
# tasks 字典现在也存储 Popen 进程对象，以便后续清理
tasks = {}
# TiDB 编译后的二进制文件相对路径
TIDB_BINARY_PATH = "bin/tidb-server"
# 编译命令
COMPILE_COMMAND = "make"
TIDB_REPO_PATH = '/Users/lt/git/tidb'

# --- commit 二分查找函数 --
def run_command(command, work_dir=".", shell=False, check=True):
    """一个通用的命令执行函数，实时打印输出"""
    print(f"🚀 在 '{work_dir}' 中执行: {' '.join(command) if isinstance(command, list) else command}")
    try:
        process = subprocess.Popen(
            command,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            shell=shell
        )

        output_lines = []
        for line in iter(process.stdout.readline, ''):
            sys.stdout.write(line)
            output_lines.append(line)

        process.wait()

        if check and process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command)

        return "".join(output_lines)
    except FileNotFoundError:
        print(f"❌ 命令未找到: {command[0]}. 请确保它已安装并在您的 PATH 中。")
        sys.exit(1)

def get_commit_list(start_tag, end_tag,task_id):
    """获取两个 tag 之间的 commit SHA 列表，按时间正序排列"""
    tasks[task_id]['log'].append(f"\nℹ️ 准备切换到与 tag '{end_tag}' 相关的 release 分支...")
    try:
        # 从 'v8.5.0' -> '8.5.0' -> ['8', '5', '0'] -> '8.5'
        version_parts = end_tag.lstrip('v').split('.')
        branch_version = f"{version_parts[0]}.{version_parts[1]}"
        branch_name = f"release-{branch_version}"

        # 使用 -f 强制切换，忽略本地未提交的修改
        run_command(["git", "checkout", "-f", branch_name], work_dir=TIDB_REPO_PATH)
        tasks[task_id]['log'].append(f"✅ 成功切换到分支: {branch_name}")

    except IndexError:
        tasks[task_id]['log'].append(f"⚠️ 警告: 无法从 tag '{end_tag}' 推断出标准的 release 分支名。跳过 checkout。")
    except subprocess.CalledProcessError:
        tasks[task_id]['log'].append(f"⚠️ 警告: 切换到分支 '{branch_name}' 失败。该分支可能在本地不存在。")
        tasks[task_id]['log'].append("继续尝试直接使用 tag 进行 commit 查找...")

    command = ["git", "rev-list", "--reverse", f"{start_tag}...{end_tag}"]
    output = run_command(command, TIDB_REPO_PATH)
    tasks[task_id]['log'].append(f"\n🔍 获取 {start_tag}..{end_tag} 之间的 commit 列表...")
    # --reverse 参数让 commit 从旧到新排列，符合二分查找的逻辑顺序
    command = ["git", "rev-list", "--reverse", f"{start_tag}...{end_tag}"]
    output = run_command(command, TIDB_REPO_PATH)
    commits = output.strip().split('\n')
    tasks[task_id]['log'].append(f"✅ 找到 {len(commits)} 个 commits。")
    return [c for c in commits if c] # 过滤空行


def compile_at_commit(commit_sha,task_id):
    """Checkout 到指定 commit 并进行编译"""
    tasks[task_id]['log'].append(f"\n🔧 切换到 commit: {commit_sha[:8]} 并开始编译...")
    try:
        run_command(["git", "checkout", commit_sha], work_dir=TIDB_REPO_PATH)
        # 编译 TiDB server
        run_command(COMPILE_COMMAND.split(), work_dir=TIDB_REPO_PATH)

        binary_full_path = os.path.join(TIDB_REPO_PATH, TIDB_BINARY_PATH)
        if not os.path.exists(binary_full_path):
            raise FileNotFoundError(f"编译产物 {binary_full_path} 未找到！")

        tasks[task_id]['log'].append(f"✅ 编译成功: {binary_full_path}")
        return binary_full_path
    except subprocess.CalledProcessError as e:
        tasks[task_id]['log'].append(f"❌ 在 commit {commit_sha[:8]} 编译失败！")
        # 在二分查找中，编译失败通常被视为 "bad" commit
        return None

# --- 辅助函数 ---

def get_tidb_versions():
    """通过 tiup list tidb 获取可用的 TiDB 版本列表"""
    try:
        subprocess.run(["tiup", "update", "--self"], check=True, capture_output=True, text=True, timeout=120)
        result = subprocess.run(
            ["tiup", "list", "tidb"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60
        )
        versions = []
        for line in result.stdout.splitlines():
            if line.strip().startswith('v') and 'Available versions' not in line and '---' not in line:
                version = line.split()[0]
                if all(c in 'v0123456789.' for c in version):
                    versions.append(version)
        versions.sort(key=Version, reverse=True)
        # versions.insert(0, "nightly")
        return versions

    except Exception as e:
        print(f"获取 TiDB 版本失败: {e}")
        return ["v8.1.0", "v8.0.0", "v7.5.1", "v7.1.3", "v6.5.9", "v6.1.7", "v5.4.3", "v4.0.16"]


def run_sql_on_tidb(sql, port):
    """在指定的 TiDB 实例上执行 SQL"""
    result_str = ""
    try:
        conn = mysql.connector.connect(
            host='127.0.0.1',
            port=port,
            user='root',
            password='',
            database='test',
            connection_timeout=20
        )
        cursor = conn.cursor()
        for stmt in sql.split(';'):
            if stmt.strip():
                cursor.execute(stmt)
                if cursor.with_rows:
                    rows = cursor.fetchall()
                    result_str += str(rows) + "\n"
        conn.commit()
        cursor.close()
        conn.close()
        return result_str.strip(), True
    except mysql.connector.Error as err:
        print(f"SQL 执行失败")
        return str(err), False


def test_single_version(version, sql, expected_result, task_id, index, cleanup_after=False, commit=''):
    """使用 tiup playground 启动一个 TiDB 集群并执行测试"""
    port_offset = random.randint(10000, 30000)
    sql_port = 4000 + port_offset
    dashboard_port = 2379 + port_offset

    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_filename = f"{log_dir}/task_{task_id[:8]}_{version}.log"

    if commit != '':
        log_message = f"commit {commit}: 准备启动集群 (端口偏移: {port_offset}, SQL Port: {sql_port})..."
    else:
        log_message = f"版本 {version}: 准备启动集群 (端口偏移: {port_offset}, SQL Port: {sql_port})..."

    tasks[task_id]['log'].append(log_message)

    process = None
    log_file = None
    try:
        log_file = open(log_filename, 'w', encoding='utf-8')
        if commit != '':
            binary_full_path = os.path.join(TIDB_REPO_PATH, TIDB_BINARY_PATH)
            cmd = ['tiup', 'playground', f'--db.binpath={binary_full_path}', version, f'--port-offset={port_offset}', '--without-monitor',"--kv", "3", "--tiflash", "1"]
            print("install use a binary")
        else:
            cmd = ['tiup', 'playground', version, f'--port-offset={port_offset}', '--without-monitor', "--kv", "3",
                   "--tiflash", "1"]
            # print("install use a version")

        # 使用 Popen 启动非阻塞的子进程
        process = subprocess.Popen(cmd, stdout=log_file, stderr=log_file, text=True, encoding='utf-8')

        # 将进程对象和版本信息存入 task，以便后续清理
        tasks[task_id]['processes'].append({'version': version, 'process': process, 'offset': port_offset,'log_file': log_filename})

        if commit != '':
            log_message = f"commit {commit}: 集群进程已启动 (PID: {process.pid})，等待 TiDB 服务就绪..."
        else:
            log_message = f"版本 {version}: 集群进程已启动 (PID: {process.pid})，等待 TiDB 服务就绪..."
        tasks[task_id]['log'].append(log_message)

        ready = False
        # 等待 TiDB 准备就绪，最多等待 180 秒
        for _ in range(36):
            time.sleep(5)
            try:
                conn = mysql.connector.connect(host='127.0.0.1', port=sql_port, user='root', password='',
                                               connection_timeout=5)
                conn.close()
                ready = True
                if commit != '':
                    log_message = f"commit {commit}: TiDB 服务在端口 {sql_port} 上已就绪。"
                else:
                    log_message = f"版本 {version}: TiDB 服务在端口 {sql_port} 上已就绪。"
                tasks[task_id]['log'].append(log_message)
                break
            except mysql.connector.Error:
                # 检查进程是否意外退出
                if process.poll() is not None:
                    raise Exception(f"TiUP 进程意外退出。Stderr: {process.stderr.read()}")
                continue

        if not ready:
            raise Exception("TiDB 服务启动超时")

        # 如果上commit 定位过程，需要在测试前确认一下commit是否一致。
        if commit != '':
            sql_check = 'select tidb_version();'
            v_result, success = run_sql_on_tidb(sql_check, sql_port)
            if commit not in ''.join(v_result.split()):
                tasks[task_id]['log'].append("tidb binary is not correct")
                raise Exception(f"TiUP 进程意外退出。Stderr: {process.stderr.read()}")
            tasks[task_id]['log'].append("check tidb binary version pass.")

        actual_result, success = run_sql_on_tidb(sql, sql_port)

        if ''.join(expected_result.split()) in ''.join(actual_result.split()):
            status = "成功"
        else:
            status = "失败"
        if commit != '':
            version_commit = f"{version}-{commit}"
            result_data = {
                'version': version_commit, 'status': status, 'sql_port': sql_port,
                'dashboard_port': dashboard_port, 'expected': expected_result, 'actual': actual_result
            }
        else:
            result_data = {
                'version': version, 'status': status, 'sql_port': sql_port,
                'dashboard_port': dashboard_port, 'expected': expected_result, 'actual': actual_result
            }

    except Exception as e:
        error_msg = f"测试版本 {version} 时发生错误: {e}"
        tasks[task_id]['log'].append(error_msg)
        result_data = {'version': version, 'status': '失败', 'error': str(e)}
    finally:
        # 在二分查找模式下，测试完一个版本就立即清理
        if log_file:
            log_file.close() # 关闭日志文件句柄
        if cleanup_after and process:
            if commit != '':
                tasks[task_id]['log'].append(f"commit {commit}: 测试完成，清理集群 (PID: {process.pid})...")
            else:
                tasks[task_id]['log'].append(f"版本 {commit}: 测试完成，清理集群 (PID: {process.pid})...")
            process.terminate()
            process.wait()

    tasks[task_id]['results'][index] = result_data


# --- 路由 ---

@app.route('/')
def index():
    versions = get_tidb_versions()
    return render_template('index.html', versions=versions)


@app.route('/locate')
def locate():
    return render_template('locate.html')


@app.route('/start_test', methods=['POST'])
def start_test():
    data = request.json
    selected_versions = data.get('versions', [])
    sql = data.get('sql')
    expected_result = data.get('expected')

    task_id = str(uuid4())
    tasks[task_id] = {
        'status': 'running', 'log': [],
        'results': [{} for _ in selected_versions],  # 占位
        'processes': [], 'type': 'test'
    }
    session.setdefault('task_ids', []).append(task_id)
    session.modified = True

    threads = []
    for i, version in enumerate(selected_versions):
        thread = threading.Thread(target=test_single_version, args=(version, sql, expected_result, task_id, i, False))
        threads.append(thread)
        thread.start()

    def wait_for_completion():
        for t in threads:
            t.join()
        tasks[task_id]['status'] = 'complete'

    threading.Thread(target=wait_for_completion).start()

    return jsonify({'task_id': task_id})


def run_binary_search(start_v_str, end_v_str, sql, expected, task_id):
    """二分查找逻辑"""
    all_versions = get_tidb_versions()

    def commit_binary_search_logic(start_version, end_version):
        commits = get_commit_list(start_version, end_version, task_id)
        if not commits:
            print("在指定的 tag 范围内未找到任何 commit。")
            return

        tasks[task_id]['log'].append(f"开始在 {commits[0]} 到 {commits[-1]} 之间进行二分查找...")

        low, high = 0, len(commits) - 1
        first_bad_commit = None

        while low <= high:
            mid = (low + high) // 2
            commit_sha = commits[mid]

            tasks[task_id]['log'].append(f"\n--- 正在测试第 {mid + 1}/{len(commits)} 个 commit: {commit_sha[:12]} ---")
            binary_path = compile_at_commit(commit_sha, task_id)
            if binary_path is None:
                print(f"👎 [BAD] Commit {commit_sha[:12]} 编译失败。")
                first_bad_commit = commit_sha
                high = mid - 1
                continue
            result_index = len(tasks[task_id]['results'])
            tasks[task_id]['results'].append({})  # 占位
            # cleanup_after=True 表示测试完就清理
            test_single_version(end_version, sql, expected, task_id, result_index, cleanup_after=True,
                                commit=commit_sha)

            result_data = tasks[task_id]['results'][result_index]

            if result_data.get('status') == '失败':
                first_bad_commit = commit_sha
                high = mid - 1
            elif result_data.get('status') == '成功':
                low = mid + 1
            else:
                tasks[task_id]['log'].append(f"版本 {commit_sha} 测试时发生环境错误，定位中止。")
                tasks[task_id]['status'] = 'error'
                return None

        return first_bad_commit

    def binary_search_logic(start_version, end_version):
        search_space = [
            v for v in all_versions
            if Version(v) >= Version(start_version) and Version(v) <= Version(end_version)
        ]
        search_space.sort(key=Version)

        tasks[task_id]['log'].append(f"开始在 {start_version} 到 {end_version} 之间进行二分查找...")
        tasks[task_id]['log'].append(f"待查找的版本列表: {search_space}")

        low, high = 0, len(search_space) - 1
        first_bad_version = None

        while low <= high:
            mid_idx = (low + high) // 2
            version_to_test = search_space[mid_idx]

            result_index = len(tasks[task_id]['results'])
            tasks[task_id]['results'].append({})  # 占位
            # cleanup_after=True 表示测试完就清理
            test_single_version(version_to_test, sql, expected, task_id, result_index, cleanup_after=True)

            result_data = tasks[task_id]['results'][result_index]

            if result_data.get('status') == '失败':
                first_bad_version = version_to_test
                high = mid_idx - 1
            elif result_data.get('status') == '成功':
                low = mid_idx + 1
            else:
                tasks[task_id]['log'].append(f"版本 {version_to_test} 测试时发生环境错误，定位中止。")
                tasks[task_id]['status'] = 'error'
                return None

        return first_bad_version

    # 基线版本测试
    if start_v_str == "v5.4.0":
        tasks[task_id]['log'].append("检查基线版本 v5.4.0...")
        # 【修复】为 v5.4.0 测试添加占位符
        tasks[task_id]['results'].append({})
        test_single_version("v5.4.0", sql, expected, task_id, 0)
        # 确保测试线程有时间写入结果
        time.sleep(0.1)
        v540_result = tasks[task_id]['results'][0]

        if v540_result.get('status') == '失败' and 'error' not in v540_result:
            tasks[task_id]['log'].append("v5.4.0 上的结果已不符合预期，将在 v3.0.0 和 v5.3.0 之间查找。")
            start_v_str = "v3.0.0"
            end_v_str = "v5.3.0"
        elif v540_result.get('status') == '成功':
            start_v_str = "v5.4.1"

    found_version = binary_search_logic(start_v_str, end_v_str)
    tasks[task_id][
        'final_result'] = f"定位到第一个出错的版本是: {found_version}" if found_version else f"在 {start_v_str}-{end_v_str} 范围内未找到不符合预期的版本。"

    if found_version:
        tidb_versions = get_tidb_versions()
        start_version_index = tidb_versions.index(found_version) + 1
        found_commit = commit_binary_search_logic(tidb_versions[start_version_index], found_version)
        tasks[task_id][
            'final_result'] = f"定位到第一个出错的commit是: {found_version}-{found_commit}, " if found_commit else f"在 {start_v_str} 范围内未找到不符合预期的commit。"

    tasks[task_id]['status'] = 'complete'


@app.route('/start_locate', methods=['POST'])
def start_locate():
    data = request.json
    bug_version = data.get('bug_version')
    start_version_str = data.get('start_version') or "v5.4.0"
    sql = data.get('sql')
    expected_result = data.get('expected')

    if not bug_version:
        return jsonify({'error': '“bug 上报版本”不能为空'}), 400
    try:
        if Version(start_version_str) >= Version(bug_version):
            return jsonify({'error': '“起始版本”不能等于或者晚于“bug 上报版本”'}), 400
    except Exception:
        return jsonify({'error': '版本号格式无效'}), 400

    task_id = str(uuid4())
    tasks[task_id] = {
        'status': 'running', 'log': [], 'results': [],
        'processes': [], 'type': 'locate'
    }
    session.setdefault('task_ids', []).append(task_id)
    session.modified = True

    thread = threading.Thread(target=run_binary_search,
                              args=(start_version_str, bug_version, sql, expected_result, task_id))
    thread.start()

    return jsonify({'task_id': task_id})


@app.route('/status/<task_id>')
@app.route('/status/<task_id>')
def task_status(task_id):
    """获取任务状态 (修正版)"""
    task = tasks.get(task_id)
    if not task:
        return jsonify({'status': 'not_found'}), 404

    # 创建一个可序列化（可转换为 JSON）的任务副本
    # 不要直接修改原始的 task 字典，因为我们还需要里面的 Popen 对象来清理进程
    serializable_task = {
        'status': task.get('status'),
        'log': task.get('log', []),
        'results': task.get('results', []),
        'type': task.get('type'),
        'final_result': task.get('final_result'),
        'processes_info': [] # 创建一个新的列表来存放进程信息
    }

    # 遍历原始任务中的进程列表
    if 'processes' in task:
        for proc_info in task['processes']:
            process = proc_info.get('process')
            # 将 Popen 对象替换为它的 PID (一个简单的整数)
            serializable_task['processes_info'].append({
                'version': proc_info.get('version'),
                'pid': process.pid if process else None,
                'offset': proc_info.get('offset'),
                # 检查进程是否还在运行
                'is_running': process.poll() is None if process else False
            })

    # 返回这个清理过后的、安全的字典
    return jsonify(serializable_task)


@app.route('/clean', methods=['POST'])
def clean_env():
    """清理当前 session 创建的所有 tiup playground 进程和日志文件 (更新版)"""
    # This line is at the first level of indentation
    task_ids_to_clean = session.get('task_ids', [])
    cleaned_pids = []
    deleted_logs = []
    errors = []

    # The 'for' loop is at the first level
    for task_id in task_ids_to_clean:
        # This block is at the second level of indentation
        task = tasks.get(task_id)
        if not task or not task.get('processes'):
            continue
        
        # This 'for' loop is at the second level
        for proc_info in task['processes']:
            # This block is at the third level of indentation
            # 1. 终止进程
            process = proc_info.get('process')
            if process and process.poll() is None:
                # This block is at the fourth level
                try:
                    pid = process.pid
                    process.terminate()
                    process.wait(timeout=10)
                    cleaned_pids.append(pid)
                except Exception as e:
                    errors.append(f"清理进程 PID {pid} 失败: {e}")
            
            # 2. 删除日志文件 (This block is at the third level)
            log_file = proc_info.get('log_file')
            if log_file and os.path.exists(log_file): # <-- This is the corrected line
                # This block is at the fourth level
                try:
                    os.remove(log_file)
                    deleted_logs.append(log_file)
                except OSError as e:
                    errors.append(f"删除日志文件 {log_file} 失败: {e}")

    # This block is back at the first level
    session['task_ids'] = []
    session.modified = True
    
    return jsonify({
        'cleaned_pids': cleaned_pids,
        'deleted_logs': deleted_logs,
        'errors': errors
    })

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
