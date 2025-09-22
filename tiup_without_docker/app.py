import os
import subprocess
import random
import time
import threading
from uuid import uuid4
from flask import Flask, render_template, request, jsonify, session, send_from_directory
from packaging.version import Version
import mysql.connector
import sys
import stat
import ast

TIDB_GO_VERSION_MAP = {
    "4.0": "1.13.15",
    "5.0": "1.13.15",
    "5.1": "1.16.15",
    "5.2": "1.16.15",
    "5.3": "1.16.15",
    "5.4": "1.16.15",
    "6.0": "1.18.10",
    "6.1": "1.18.10",
    "6.2": "1.18.10",
    "6.3": "1.19.13",
    "6.4": "1.19.13",
    "6.5": "1.19.13",
    "6.6": "1.19.13",
    "7.0": "1.20.14",
    "7.1": "1.20.14",
    "7.2": "1.20.14",
    "7.3": "1.20.14",
    "7.4": "1.21.13",
    "7.5": "1.21.13",
    "7.6": "1.21.13",
    "8.0": "1.21.13",
    "8.1": "1.21.13",
    "8.2": "1.21.13",
    "8.3": "1.21.13",
    "8.4": "1.23.6",
    "8.5": "1.25.1",
    # 您可以根据需要继续添加新的版本映射
}
DEFAULT_GO_VERSION = "1.25.1"

COMPONENT_COUNTS = {
    'tidb': 1,
    'tikv': 1,
    'pd': 1,
    'tiflash': 0
}

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
def run_command(command, work_dir=".", shell=False, check=True, print_output=False):
    """一个通用的命令执行函数，实时打印输出"""
    print(f"🚀 在 '{work_dir}' 中执行: {' '.join(command) if isinstance(command, list) else command}")

    if isinstance(command, list):
        # 将列表命令安全地拼接成字符串
        command_str = ' '.join(f"'{arg}'" if ' ' in arg else arg for arg in command)
    else:
        command_str = command

    asdf_script_path = os.path.expanduser("~/.asdf/asdf.sh")
    if not os.path.exists(asdf_script_path):
        print(f"❌ 错误: asdf 环境脚本未在 '{asdf_script_path}' 找到。")
        sys.exit(1)
    # 使用 bash -c '...' 来确保在一个 shell 中先 source 再执行命令
    final_command = f". {asdf_script_path} && {command_str}"
    final_command_list = ["/bin/bash", "-li", "-c", final_command]

    if print_output:
        print(f"🚀 (In Bash with ASDF Env) 在 '{work_dir}' 中执行: {final_command}")

    try:
        process = subprocess.Popen(
            final_command_list,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            shell=shell,
            preexec_fn=os.setsid if sys.platform != "win32" else None
        )

        output_lines, full_output = [], ""
        if print_output:
            for line in iter(process.stdout.readline, ''):
                sys.stdout.write(line)
                output_lines.append(line)
            full_output = "".join(output_lines)

        process.wait()

        if not print_output:
            full_output = process.stdout.read()

        if check and process.returncode != 0:
            print("compile fail:", process.stderr)
            raise subprocess.CalledProcessError(process.returncode, command)

        return full_output
    except FileNotFoundError:
        command_name = command[0] if isinstance(command, list) else command.split()[0]
        print(f"❌ 命令未找到: {command[0]}. 请确保它已安装并在您的 PATH 中。")
        sys.exit(1)


def get_commit_list(start_tag, end_tag, task_id):
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
        return
    except subprocess.CalledProcessError:
        tasks[task_id]['log'].append(f"⚠️ 警告: 切换到分支 '{branch_name}' 失败。该分支可能在本地不存在。")
        tasks[task_id]['log'].append("继续尝试直接使用 tag 进行 commit 查找...")
        return

    try:
        command = ["git", "rev-list", "--reverse", f"{start_tag}..{end_tag}"]
        output = run_command(command, TIDB_REPO_PATH)
        tasks[task_id]['log'].append(f"\n🔍 获取 {start_tag}..{end_tag} 之间的 commit 列表...")
        # --reverse 参数让 commit 从旧到新排列，符合二分查找的逻辑顺序
        commits = output.strip().split('\n')
        tasks[task_id]['log'].append(f"✅ 找到 {len(commits)} 个 commits。")
        return [c for c in commits if c]
    except Exception as e:
        tasks[task_id]['log'].append(f"❌ get commits list fail")
        # 在二分查找中，编译失败通常被视为 "bad" commit
        return None


def compile_at_commit(commit_sha, task_id, version):
    """Checkout 到指定 commit 并进行编译"""
    tasks[task_id]['log'].append(f"\n🔧 切换到 commit: {commit_sha[:8]} 并开始编译...")
    try:
        if version == 'master':
            go_version = DEFAULT_GO_VERSION
        else:
            version_key = ".".join(version.lstrip('v').split('.')[:2])
            go_version = TIDB_GO_VERSION_MAP.get(version_key, DEFAULT_GO_VERSION)

            if version_key not in TIDB_GO_VERSION_MAP:
                print(f"⚠️ 警告: 在版本映射中未找到 '{version_key}'。将使用默认 Go 版本: {DEFAULT_GO_VERSION}")

        run_command(["git", "checkout", commit_sha], work_dir=TIDB_REPO_PATH)

        print(f"⚙️ 正在为 TiDB 版本 '{version}' 设置 Go 版本为: {go_version}...")
        run_command(["asdf", "local", "go", go_version], work_dir=TIDB_REPO_PATH)

        # 验证 Go 版本是否切换成功
        print("Verifying Go version...")
        run_command(["go", "version"], work_dir=TIDB_REPO_PATH)
    except Exception as e:
        print(f"❌ 设置 Go 版本时出错: {e}。将使用环境中已有的 Go 版本继续尝试。")
        return

    try:
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
                    if len(version) <= 4:
                        continue
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
            autocommit=True,
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
        return result_str, True
    except mysql.connector.Error as err:
        print(f"SQL 执行失败")
        return str(err), False


def run_other_check(script_content, port, task_id):
    """执行其他检查脚本"""
    tasks[task_id]['log'].append("--- 开始其他检查 ---")

    # 1. 获取 TiDB 日志目录
    log_dir_query = "show config where type='tidb' and name='log.file.filename';"
    try:
        result, success = run_sql_on_tidb(log_dir_query, port)
        if not success or not result:
            msg = "获取 TiDB 日志目录失败。"
            tasks[task_id]['log'].append(f"❌ {msg}")
            return "Failure", msg

        try:
            data_list = ast.literal_eval(result)
            log_file_path = data_list[0][3]
        except (ValueError, SyntaxError) as e:
            print(f"解析字符串时出错: {e}")

        tasks[task_id]['log'].append(f"✅ 成功获取到tidb日志目录: {log_file_path}")
        # e.g., /Users/lt/.tiup/data/Ux1ux8z/tidb-0/tidb.log -> /Users/lt/.tiup/data/Ux1ux8z/
        base_dir = os.path.dirname(os.path.dirname(log_file_path))
        tasks[task_id]['log'].append(f"✅ 脚本将会在此基础目录执行: {base_dir}")

    except Exception as e:
        msg = f"解析 TiDB 日志目录时出错: {e}"
        tasks[task_id]['log'].append(f"❌ {msg}")
        return "Failure", msg

    # 2. 保存并执行脚本
    script_path = os.path.join(base_dir, f"check_script_{task_id[:8]}.sh")
    try:
        with open(script_path, 'w') as f:
            f.write("#!/bin/bash\n")
            f.write(script_content)

        # 赋予脚本执行权限
        st = os.stat(script_path)
        os.chmod(script_path, st.st_mode | stat.S_IEXEC)
        tasks[task_id]['log'].append(f"✅ 检查脚本已保存到: {script_path}")

        # 执行脚本
        tasks[task_id]['log'].append(f"🚀 执行检查脚本...")
        process = subprocess.run(
            ['/bin/bash', script_path],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=base_dir
        )

        script_output = process.stdout.strip() + "\n" + process.stderr.strip()
        tasks[task_id]['log'].append(f"脚本输出:\n{script_output}")
        print("return code:",process.returncode)

        # 3. 根据返回值判断结果
        if process.returncode == 0:
            tasks[task_id]['log'].append("✅ 其他检查通过 (脚本返回值为 0)。")
            return "Success", script_output
        else:
            tasks[task_id]['log'].append(f"❌ 其他检查失败 (脚本返回值为 {process.returncode})。")
            return "Failure", script_output

    except Exception as e:
        msg = f"执行检查脚本时发生严重错误: {e}"
        tasks[task_id]['log'].append(f"❌ {msg}")
        return "Failure", msg
    finally:
        # 清理脚本文件
        if os.path.exists(script_path):
            os.remove(script_path)


def test_single_version(version, sql, expected_sql_result, other_check_script, task_id, index, cleanup_after=False,
                        commit=''):
    """使用 tiup playground 启动一个 TiDB 集群并执行测试"""
    port_offset = random.randint(10000, 30000)
    sql_port = 4000 + port_offset
    dashboard_port = 2379 + port_offset

    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_filename = f"{log_dir}/task_{task_id[:8]}_{version}.log"
    tasks[task_id]['log'].append(f"task_id: {task_id[:8]}")

    if commit != '':
        log_message = f"commit {commit}: 准备启动集群 (端口偏移: {port_offset}, SQL Port: {sql_port})..."
    else:
        log_message = f"版本 {version}: 准备启动集群 (端口偏移: {port_offset}, SQL Port: {sql_port})..."

    tasks[task_id]['log'].append(log_message)

    process = None
    log_file = None
    tidb_number = COMPONENT_COUNTS['tidb']
    tikv_number = COMPONENT_COUNTS['tikv']
    pd_number = COMPONENT_COUNTS['pd']
    tiflash_number = COMPONENT_COUNTS['tiflash']

    if commit:
        version_commit = f"{version}-{commit}"
        result_data = {'version': version_commit}
    else:
        result_data = {'version': version}

    try:
        log_file = open(log_filename, 'w', encoding='utf-8')
        if commit != '':
            binary_full_path = os.path.join(TIDB_REPO_PATH, TIDB_BINARY_PATH)
            cmd = ['tiup', 'playground', f'--db.binpath={binary_full_path}', version, f'--port-offset={port_offset}',
                   '--without-monitor', '--kv', f'{tikv_number}', '--tiflash', f'{tiflash_number}',
                   '--pd', f'{pd_number}', '--db', f'{tidb_number}']
            print("install binary cmd:", cmd)
        else:
            cmd = ['tiup', 'playground', version, f'--port-offset={port_offset}', '--without-monitor',
                   '--kv', f'{tikv_number}', '--tiflash', f'{tiflash_number}',
                   '--pd', f'{pd_number}', '--db', f'{tidb_number}']
            print("install version cmd:", cmd)

        # 使用 Popen 启动非阻塞的子进程
        process = subprocess.Popen(cmd, stdout=log_file, stderr=log_file, text=True, encoding='utf-8')

        # 将进程对象和版本信息存入 task，以便后续清理
        tasks[task_id]['processes'].append({'version': version, 'process': process, 'offset': port_offset, 'log_file': log_filename})

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

            if not success or commit not in ''.join(v_result.split()):
                raise Exception(f"TiDB binary 版本不正确! 期望包含 {commit}, 实际为 {v_result}")
            tasks[task_id]['log'].append("✅ TiDB binary 版本检查通过。")

        # --- 执行检查 ---
        sql_check_passed = None
        other_check_passed = None

        # 1. SQL 结果检查
        if expected_sql_result is not None:
            actual_sql_resultstr, success = run_sql_on_tidb(sql, sql_port)
            print("actual result:", actual_sql_resultstr)
            result_data.update({'expected_sql': expected_sql_result, 'actual_sql': actual_sql_resultstr})

            if expected_sql_result.strip():
                if ''.join(expected_sql_result.split()) in ''.join(actual_sql_resultstr.split()):
                    sql_check_passed = True
                else:
                    sql_check_passed = False
            else:
                # if expected is empty，then sql executed success means check pass.
                if success:
                    print("expected sql is empty, and sql executed success")
                    sql_check_passed = True
                else: sql_check_passed = False
        # 2. 其他检查
        if other_check_script.strip():
            other_status, other_output = run_other_check(other_check_script, sql_port, task_id)
            result_data.update({'other_check_status': other_status, 'other_check_output': other_output})
            other_check_passed = (other_status == "Success")
        # 3. 综合判断最终结果
        if sql_check_passed is None and other_check_passed is None:
            # This case is pre-validated in start_locate, but as a safeguard
            raise Exception("没有提供任何检查标准。")

        final_status = "Success"  # Assume success
        if sql_check_passed is False or other_check_passed is False:
            final_status = "Failure"

        result_data.update({
                'status': final_status,
                'sql_port': sql_port,
                'dashboard_port': dashboard_port
        })
    except Exception as e:
        error_msg = f"测试版本 {version} 时发生错误: {e}"
        tasks[task_id]['log'].append(error_msg)
        result_data = {'version': version, 'status': 'Failure', 'error': str(e)}
    finally:
        # 在二分查找模式下，测试完一个版本就立即清理
        if log_file:
            log_file.close()
        if cleanup_after and process:
            if commit != '':
                tasks[task_id]['log'].append(f"commit {commit}: 测试完成，清理集群 (PID: {process.pid})...")
            else:
                tasks[task_id]['log'].append(f"版本 {commit}: 测试完成，清理集群 (PID: {process.pid})...")
            process.terminate()
            process.wait()

    tasks[task_id]['results'][index] = result_data


# --- 路由 ---
@app.route('/locales/<path:filename>')
def serve_locales(filename):
    """This route serves static files from the 'locales' directory."""
    # Now that it's imported, this function call will work correctly.
    return send_from_directory(os.path.join(app.root_path, 'locales'), filename)


@app.route('/')
def index():
    versions = get_tidb_versions()
    return render_template('index.html', versions=versions)


@app.route('/locate')
def locate_page():
    return render_template('locate.html')


@app.route('/start_test', methods=['POST'])
def start_test():
    global COMPONENT_COUNTS

    data = request.json
    selected_versions = data.get('versions', [])
    sql = data.get('sql')
    print("testcase:", sql)
    expected_sql = data.get('expected_sql_result', '').strip()
    other_script = data.get('other_check_script', '').strip()

    if not selected_versions:
        return jsonify({'error': '请至少选择一个版本。'}), 400

    tidb_count = int(data.get('tidb') or COMPONENT_COUNTS['tidb'])
    tikv_count = int(data.get('tikv') or COMPONENT_COUNTS['tikv'])
    pd_count = int(data.get('pd') or COMPONENT_COUNTS['pd'])
    tiflash_count = int(data.get('tiflash') or COMPONENT_COUNTS['tiflash'])

    COMPONENT_COUNTS = {
        'tidb': tidb_count,
        'tikv': tikv_count,
        'pd': pd_count,
        'tiflash': tiflash_count
    }
    print(f"[*] Global component counts have been updated to: {COMPONENT_COUNTS}")

    print("收到定位任务请求:")
    print(f"  - Bug 版本: {selected_versions}")
    print(f"  - SQL: {sql}")
    print(f"  - 预期结果: {expected_sql}")

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
        thread = threading.Thread(target=test_single_version, args=(version, sql, expected_sql, other_script, task_id, i, False, ''))
        threads.append(thread)
        thread.start()

    def wait_for_completion():
        for t in threads:
            t.join()
        tasks[task_id]['status'] = 'complete'

    threading.Thread(target=wait_for_completion).start()

    return jsonify({'task_id': task_id})


def run_binary_search_with_version(start_v_str, end_v_str, sql, expected_sql, other_check, task_id):
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
            binary_path = compile_at_commit(commit_sha, task_id, end_version)
            if binary_path is None:
                print(f"👎 [BAD] Commit {commit_sha[:12]} 编译失败。")
                # first_bad_commit = commit_sha
                high = mid - 1
                continue
            result_index = len(tasks[task_id]['results'])
            tasks[task_id]['results'].append({})
            test_single_version(end_version, sql, expected_sql, other_check, task_id, result_index, cleanup_after=True,
                                commit=commit_sha)

            result_data = tasks[task_id]['results'][result_index]

            if result_data.get('status') == 'Failure':
                first_bad_commit = commit_sha
                high = mid - 1
            elif result_data.get('status') == 'Success':
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
            tasks[task_id]['results'].append({})
            test_single_version(version_to_test, sql, expected_sql, other_check, task_id, result_index,
                                cleanup_after=True)

            result_data = tasks[task_id]['results'][result_index]

            if result_data.get('status') == 'Failure':
                first_bad_version = version_to_test
                high = mid_idx - 1
            elif result_data.get('status') == 'Success':
                low = mid_idx + 1
            else:
                tasks[task_id]['log'].append(f"版本 {version_to_test} 测试时发生环境错误，定位中止。")
                tasks[task_id]['status'] = 'error'
                return None

        return first_bad_version

    # 1. 对起始版本进行基线检查
    tasks[task_id]['log'].append(f"\n--- 正在执行基线检查 (起始点): {start_v_str} ---")
    start_index = len(tasks[task_id]['results'])
    tasks[task_id]['results'].append({})
    test_single_version(start_v_str, sql, expected_sql, other_check, task_id, start_index, cleanup_after=True)

    start_result = tasks[task_id]['results'][start_index]
    if start_result.get('status') == 'Failure':
        error_msg = "本范围内无法找到引入问题的 pr,请在更早的版本或者 commit 范围内查找"
        tasks[task_id]['log'].append(f"\n❌ 基线检查失败: 起始版本 {start_v_str} 已不符合预期。")
        tasks[task_id]['final_result'] = error_msg
        tasks[task_id]['status'] = 'complete'
        return

    # 2. 对结束版本（Bug上报版本）进行健全性检查
    tasks[task_id]['log'].append(f"\n--- 正在执行健全性检查 (结束点): {end_v_str} ---")
    end_index = len(tasks[task_id]['results'])
    tasks[task_id]['results'].append({})
    test_single_version(end_v_str, sql, expected_sql, other_check, task_id, end_index, cleanup_after=True)

    end_result = tasks[task_id]['results'][end_index]
    if end_result.get('status') == 'Success':
        error_msg = f"健全性检查失败: 'Bug 上报版本' ({end_v_str}) 的测试结果为成功，无法进行二分查找。"
        tasks[task_id]['log'].append(f"\n❌ {error_msg}")
        tasks[task_id]['final_result'] = error_msg
        tasks[task_id]['status'] = 'complete'
        return

    if start_v_str == "v5.4.0":
        tasks[task_id]['log'].append("检查基线版本 v5.4.0...")
        # 【修复】为 v5.4.0 测试添加占位符
        tasks[task_id]['results'].append({})
        test_single_version("v5.4.0", sql, expected_sql, other_check, task_id, 0)
        # 确保测试线程有时间写入结果
        time.sleep(0.1)
        v540_result = tasks[task_id]['results'][0]

        if v540_result.get('status') == 'Failure' and 'error' not in v540_result:
            tasks[task_id]['log'].append("v5.4.0 上的结果已不符合预期，将在 v4.0.0 和 v5.3.0 之间查找。")
            start_v_str = "v4.0.0"
            end_v_str = "v5.3.0"
        elif v540_result.get('status') == 'Success':
            start_v_str = "v5.4.1"

    found_version = binary_search_logic(start_v_str, end_v_str)
    tasks[task_id]['log'].append(f"\n----定位到第一个出错的版本是: {found_version}----")
    tasks[task_id][
        'final_result'] = f"定位到第一个出错的版本是: {found_version}" if found_version else f"在 {start_v_str}-{end_v_str} 范围内未找到不符合预期的版本。"

    if found_version:
        tidb_versions = get_tidb_versions()
        start_version_index = tidb_versions.index(found_version) + 1
        found_commit = commit_binary_search_logic(tidb_versions[start_version_index], found_version)
        tasks[task_id][
            'final_result'] = f"定位到第一个出错的commit是: {found_version}-{found_commit}, " if found_commit else f"在 {start_v_str} 范围内未找到不符合预期的commit。"
        if found_commit:
            try:
                output = run_command(["git", "show", found_commit, "--no-patch", ], work_dir=TIDB_REPO_PATH)
                # tasks[task_id]['log'].append(f"✅ import issue and pr: {output}")
                tasks[task_id][
                    'final_result'] = f"定位到第一个出错的commit是: {found_version}-{found_commit}\n\nimport issue and pr: {output}, " if found_commit else f"在 {start_v_str} 范围内未找到不符合预期的commit。"

            except RuntimeError as e:
                print(e)

    tasks[task_id]['status'] = 'complete'


def run_binary_search_with_commit(start_commit, end_commit, branch, sql, expected_sql, other_check, task_id):
    """二分查找逻辑"""

    def commit_binary_search_logic(start_commit, end_commit, branch):
        try:
            print(f"[*] 正在切换到分支: {branch}")
            subprocess.run(["git", "checkout", "-f", branch], cwd=TIDB_REPO_PATH, check=True, capture_output=True,
                           text=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"切换到分支 '{branch}' 失败: {e.stderr.strip()}")
            return
        command = ["git", "rev-list", "--reverse", f"{start_commit}..{end_commit}"]
        try:
            result = subprocess.run(
                command,
                cwd=TIDB_REPO_PATH,
                check=True,
                capture_output=True,
                text=True
            )

            # 按换行符分割输出，并过滤掉可能的空行
            commits_after_start = [line for line in result.stdout.strip().split('\n') if line]

            # 将起始 commit 添加到列表的开头，构成完整的包含范围
            commits = [start_commit] + commits_after_start

        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"执行 'git rev-list' 失败: {e.stderr.strip()}")

        if not commits:
            print("在指定的 tag 范围内未找到任何 commit。")
            return None

        tasks[task_id]['log'].append(f"开始在 {commits[0]} 到 {commits[-1]} 之间进行二分查找...")

        low, high = 0, len(commits) - 1
        first_bad_commit = None

        while low <= high:
            mid = (low + high) // 2
            commit_sha = commits[mid]

            tasks[task_id]['log'].append(f"\n--- 正在测试第 {mid + 1}/{len(commits)} 个 commit: {commit_sha[:12]} ---")
            i_v = branch
            if str(branch).find('release-') != -1:
                i_v = str(branch).lstrip('release-') + '.0'

            binary_path = compile_at_commit(commit_sha, task_id, i_v)
            if binary_path is None:
                print(f"👎 [BAD] Commit {commit_sha[:12]} 编译失败。")
                # first_bad_commit = commit_sha
                high = mid - 1
                continue
            result_index = len(tasks[task_id]['results'])
            tasks[task_id]['results'].append({})  # 占位
            # cleanup_after=True 表示测试完就清理
            if branch == 'master':
                install_version = 'nightly'
            elif str(branch).find('release-') != -1:
                install_version = 'v' + str(branch).lstrip('release-') + '.0'
            else:
                tasks[task_id]['log'].append(f"branch format is incorrect")
                return
            test_single_version(install_version, sql, expected_sql, other_check, task_id, result_index,
                                cleanup_after=True, commit=commit_sha)

            result_data = tasks[task_id]['results'][result_index]

            if result_data.get('status') == 'Failure':
                first_bad_commit = commit_sha
                high = mid - 1
            elif result_data.get('status') == 'Success':
                low = mid + 1
            else:
                tasks[task_id]['log'].append(f"版本 {commit_sha} 测试时发生环境错误，定位中止。")
                tasks[task_id]['status'] = 'error'
                return None

        return first_bad_commit

    # 基线版本测试
    start_commit_to_test = start_commit
    tasks[task_id]['log'].append(f"\n--- 正在执行基线检查 (起始 Commit): {start_commit_to_test[:7]} ---")

    def test_a_commit(commit_sha, index):
        install_version = 'nightly' if branch == 'master' else f'v{branch.replace("release-", "")}.0'
        binary_path = compile_at_commit(commit_sha, task_id, install_version)
        if binary_path is None:
            tasks[task_id]['results'][index] = {'version': commit_sha, 'status': 'Failure', 'error': '编译失败'}
            return
        test_single_version(install_version, sql, expected_sql, other_check, task_id, index, cleanup_after=True,
                            commit=commit_sha)

    start_index = len(tasks[task_id]['results'])
    tasks[task_id]['results'].append({})
    test_a_commit(start_commit_to_test, start_index)

    start_result = tasks[task_id]['results'][start_index]
    if start_result.get('status') == 'Failure':
        error_msg = "本范围内无法找到引入问题的pr,请在更早的版本或者commit 范围内查找"
        tasks[task_id]['log'].append(f"\n❌ 基线检查失败: 起始 Commit {start_commit_to_test[:7]} 已不符合预期。")
        tasks[task_id]['final_result'] = error_msg
        tasks[task_id]['status'] = 'complete'
        return

    # 开始二分测试
    found_commit = commit_binary_search_logic(start_commit, end_commit, branch)
    output = ""
    if found_commit:
        try:
            output = run_command(["git", "show", found_commit, "--no-patch"], work_dir=TIDB_REPO_PATH)
        except Exception as e:
            print(e)
    tasks[task_id][
        'final_result'] = f"定位到第一个出错的commit是: {found_commit}\n\nimport issue and pr: {output}, " if found_commit else f"在 {branch} 范围内未找到不符合预期的commit。"

    tasks[task_id]['status'] = 'complete'


@app.route('/start_locate', methods=['POST'])
def start_locate():
    global COMPONENT_COUNTS

    data = request.json
    if not data:
        return jsonify({'error': 'Invalid JSON payload'}), 400

    locate_mode = data.get('locate_mode')
    sql = data.get('sql')
    expected_sql_result = data.get('expected_sql_result', '').strip()
    other_check_script = data.get('other_check_script', '').strip()

    print("收到的预期 SQL 结果:", expected_sql_result)
    print("收到的其他检查脚本:", other_check_script)

    tidb_count = int(data.get('tidb') or COMPONENT_COUNTS['tidb'])
    tikv_count = int(data.get('tikv') or COMPONENT_COUNTS['tikv'])
    pd_count = int(data.get('pd') or COMPONENT_COUNTS['pd'])
    tiflash_count = int(data.get('tiflash') or COMPONENT_COUNTS['tiflash'])

    COMPONENT_COUNTS = {
        'tidb': tidb_count,
        'tikv': tikv_count,
        'pd': pd_count,
        'tiflash': tiflash_count
    }
    print(data)
    # print(f"[*] Global component counts have been updated to: {COMPONENT_COUNTS}")

    bug_version = start_version_str = branch = start_commit = end_commit = ''

    if locate_mode == 'version':
        bug_version = data.get('bug_version')
        start_version_str = data.get('start_version') or "v5.4.0"
        if not bug_version:
            return jsonify({'error': 'bug_version is required for version mode'}), 400
        try:
            if Version(start_version_str) >= Version(bug_version):
                return jsonify({'error': '“起始版本”不能等于或者晚于“bug 上报版本”'}), 400
        except Exception:
            return jsonify({'error': '版本号格式无效'}), 400
        print("收到定位任务请求:")
        print(f"  - locate mode: {locate_mode}")
        print(f"  - start version: {start_version_str}")
        print(f"  - end version: {bug_version}")
        print(f"  - SQL: {sql}")
        print(f"  - 预期结果: {expected_sql_result}")
    elif locate_mode == 'commit':
        branch = data.get('branch')
        if (branch != 'master') and (str(branch).find('release-') == -1):
            return jsonify({'error': 'branch format is incorrect, should be release-x.x'}), 400
        start_commit = data.get('start_commit')
        end_commit = data.get('end_commit')
        if not all([branch, start_commit, end_commit]):
            return jsonify({'error': 'branch, start_commit, and end_commit are required for commit mode'}), 400
        print("收到定位任务请求:")
        print(f"  - locate mode: {locate_mode}")
        print(f"  - start commit: {start_commit}")
        print(f"  - end commit: {end_commit}")
        print(f"  - branch: {branch}")
        print(f"  - SQL: {sql}")
        print(f"  - 预期结果: {expected_sql_result}")
    else:
        return jsonify({'error': f'Unknown locate_mode: {locate_mode}'}), 400

    task_id = str(uuid4())
    tasks[task_id] = {
        'status': 'running', 'log': [], 'results': [],
        'processes': [], 'type': 'locate'
    }
    session.setdefault('task_ids', []).append(task_id)
    session.modified = True

    if locate_mode == 'version':
        thread = threading.Thread(target=run_binary_search_with_version,
                              args=(start_version_str, bug_version, sql, expected_sql_result, other_check_script, task_id))
    elif locate_mode == 'commit':
        thread = threading.Thread(target=run_binary_search_with_commit,
                                  args=(start_commit, end_commit, branch, sql, expected_sql_result, other_check_script, task_id))
    thread.start()

    return jsonify({'task_id': task_id})


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
        'processes_info': []
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
                    process.wait(timeout=30)
                    cleaned_pids.append(pid)
                except Exception as e:
                    errors.append(f"清理进程 PID {pid} 失败: {e}")
            
            # 2. 删除日志文件 (This block is at the third level)
            log_file = proc_info.get('log_file')

            if log_file and os.path.exists(log_file):
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
