import os
import subprocess
import random
import time
import threading
import csv
import datetime
from uuid import uuid4
from flask import Flask, render_template, request, jsonify, session, send_from_directory
from packaging.version import Version
import mysql.connector
import sys
import stat
import ast
import shutil
from functools import wraps


def retry(max_retries=3, delay=5):
    """
    一个装饰器，用于在函数失败时自动重试。
    失败的条件是：函数抛出任何异常，或者函数返回 None。
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 尝试从参数中智能地获取 task_id 用于日志记录
            task_id = kwargs.get('task_id')
            if not task_id:
                for arg in args:
                    if isinstance(arg, str) and len(arg) > 30:  # 根据 uuid 的特征猜测 task_id
                        task_id = arg
                        break

            last_exception = None
            for attempt in range(1, max_retries + 1):
                try:
                    result = func(*args, **kwargs)
                    # 如果函数通过返回 None 来表示失败，我们也将其视为需要重试的失败
                    if result is not None:
                        return result

                    log_msg = f"⚠️ 函数 {func.__name__} 第 {attempt}/{max_retries} 次尝试失败，结果为 None。"
                    if attempt == max_retries:  # 最后一次尝试失败
                        last_exception = Exception("函数返回 None")

                except Exception as e:
                    last_exception = e
                    log_msg = f"❌ 函数 {func.__name__} 第 {attempt}/{max_retries} 次尝试失败，发生异常: {e}"

                # 记录日志
                print(log_msg)
                if task_id and task_id in tasks:
                    tasks[task_id]['log'].append(log_msg)

                if attempt < max_retries:
                    time.sleep(delay)

            # 所有重试均告失败
            final_log_msg = f"❌ 函数 {func.__name__} 在 {max_retries} 次尝试后彻底失败。最后一次错误: {last_exception}"
            print(final_log_msg)
            if task_id and task_id in tasks:
                tasks[task_id]['log'].append(final_log_msg)

            return None  # 返回 None 表示最终失败

        return wrapper

    return decorator


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
app.secret_key = 'a_very_secret_key_for_tidb_tester_tiup'

tasks = {}
TIDB_BINARY_PATH = "bin/tidb-server"  # TiDB 编译后的二进制文件相对路径
COMPILE_COMMAND = "make"  # 编译命令
# 核心代码仓库路径（作为 worktree 的源）
TIDB_REPO_PATH = '/root/git/tidb'
# 为并发任务创建隔离工作区的基准目录
# **重要**: 确保此目录存在且 Flask 应用有权读写
TIDB_WORKTREE_BASE = '/tmp/tidb_worktrees'
# 历史记录文件路径
HISTORY_LOG_FILE = 'search_history.csv'


# --- commit 二分查找函数 --
def run_command(command, work_dir=".", shell=False, check=True, print_output=False, go_version=None):
    """
    一个通用的命令执行函数，实时打印输出。
    新增 go_version 参数以支持无状态的版本切换。
    """
    print(f"🚀 在 '{work_dir}' 中执行: {' '.join(command) if isinstance(command, list) else command}")

    custom_env = os.environ.copy()
    asdf_script_path = os.path.expanduser("~/.asdf/asdf.sh")

    if not os.path.exists(asdf_script_path):
        print(f"❌ 错误: asdf 环境脚本未在 '{asdf_script_path}' 找到。")
        sys.exit(1)

    command_list = command if isinstance(command, list) else command.split()

    if go_version:
        print(f"🔧 正在为命令手动设置 Go {go_version} 环境...")
        try:
            # 1. 使用 asdf where 获取 GOROOT 路径
            asdf_where_cmd = f". {asdf_script_path} && asdf where golang {go_version}"
            go_root_path = subprocess.check_output(
                ["/bin/bash", "-li", "-c", asdf_where_cmd],
                text=True
            ).strip()

            if not go_root_path or not os.path.exists(go_root_path):
                raise FileNotFoundError(f"asdf 未能找到 Go {go_version} 的安装路径。")

            # 2. 构建 bin 目录路径
            go_bin_path = os.path.join(go_root_path, "go/bin")

            # 3. 设置 GOROOT 和 PATH 环境变量
            custom_env['GOROOT'] = os.path.join(go_root_path, "go")
            new_path = f"{go_bin_path}:{custom_env.get('PATH', '')}"
            asdf_shims_path = os.path.expanduser("~/.asdf/shims")
            path_parts = new_path.split(':')
            path_parts = [p for p in path_parts if p != asdf_shims_path]
            custom_env['PATH'] = ':'.join(path_parts)
            print(f"✅ 环境已设置: GOROOT={go_root_path}, PATH 已更新并移除了 asdf shims。")

            if command_list[0] == 'go':
                go_executable = os.path.join(go_bin_path, 'go')
                if not os.path.exists(go_executable):
                    raise FileNotFoundError(f"Go 可执行文件未在预期路径找到: {go_executable}")

                print(f"🔩 将命令 'go' 替换为绝对路径: {go_executable}")
                command_list[0] = go_executable

        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"❌ 无法为 Go {go_version} 设置环境: {e}")
            # 抛出异常以便 retry 装饰器可以捕获它
            raise RuntimeError(f"为 Go {go_version} 设置环境失败") from e

    use_shell = isinstance(command, str) and shell
    try:
        process = subprocess.Popen(
            command if use_shell else command_list,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            shell=use_shell,
            env=custom_env,  # 使用我们手动创建的环境
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
            raise subprocess.CalledProcessError(process.returncode, command, output=full_output)

        return full_output
    except FileNotFoundError:
        command_name = command[0] if isinstance(command, list) else command.split()[0]
        print(f"❌ 命令未找到: {command_name}. 请确保它已安装并在您的 PATH 中。")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"❌ 命令执行失败，返回码: {e.returncode}")
        print(f"   命令: {e.cmd}")
        print(f"   输出:\n{e.output}")
        raise


def get_commit_list(start_tag, end_tag, task_id, repo_path):
    """获取两个 tag 之间的 commit SHA 列表，在指定的 repo_path 中操作"""
    tasks[task_id]['log'].append(f"\nℹ️ 准备在隔离环境 '{repo_path}' 中切换到与 tag '{end_tag}' 相关的 release 分支...")
    try:
        version_parts = end_tag.lstrip('v').split('.')
        branch_version = f"{version_parts[0]}.{version_parts[1]}"
        branch_name = f"release-{branch_version}"
        run_command(["git", "checkout", "-f", branch_name], work_dir=repo_path)
        tasks[task_id]['log'].append(f"✅ 成功切换到分支: {branch_name}")

        tasks[task_id]['log'].append(f"🔄 正在更新仓库代码...")
        run_command(["git", "pull"], work_dir=repo_path, print_output=True)
        tasks[task_id]['log'].append(f"✅ 仓库代码已更新。")
    except IndexError:
        tasks[task_id]['log'].append(f"⚠️ 警告: 无法从 tag '{end_tag}' 推断出 release 分支名。")
    except subprocess.CalledProcessError:
        tasks[task_id]['log'].append(f"⚠️ 警告: 切换到分支 '{branch_name}' 失败。")
        return

    try:
        command = ["git", "rev-list", "--reverse", f"{start_tag}..{end_tag}"]
        output = run_command(command, repo_path)
        tasks[task_id]['log'].append(f"\n🔍 获取 {start_tag}..{end_tag} 之间的 commit 列表...")
        commits = output.strip().split('\n')
        tasks[task_id]['log'].append(f"✅ 找到 {len(commits)} 个 commits。")
        return [c for c in commits if c]
    except Exception as e:
        tasks[task_id]['log'].append(f"❌ 获取 commits 列表失败: {e}")
        return None

@retry(max_retries=3)
def compile_at_commit(commit_sha, task_id, version, repo_path):
    """在指定的隔离 repo_path 中 Checkout 到指定 commit 并进行编译"""
    tasks[task_id]['log'].append(f"\n🔧 在 '{repo_path}' 中切换到 commit: {commit_sha[:8]} 并开始编译...")
    try:
        if version == 'master' or version == 'nightly':
            go_version = DEFAULT_GO_VERSION
        else:
            version_key = ".".join(version.lstrip('v').split('.')[:2])
            go_version = TIDB_GO_VERSION_MAP.get(version_key, DEFAULT_GO_VERSION)

        tasks[task_id]['log'].append(f"🔀 切换到 commit: {commit_sha[:8]}...")
        run_command(["git", "checkout", "-f", commit_sha], work_dir=repo_path)
        tasks[task_id]['log'].append(f"✅ Git checkout 成功。")

        tasks[task_id]['log'].append(f"⚙️ 正在为 TiDB 版本 '{version}' 设置 Go 版本为: {go_version} (临时)...")

        # 验证 Go 版本是否切换成功（通过 run_command 的 go_version 参数）
        run_command(["go", "version"], work_dir=repo_path, print_output=True, go_version=go_version)

        # 编译 TiDB server，并传入 go_version
        run_command(COMPILE_COMMAND.split(), work_dir=repo_path, print_output=True, go_version=go_version)

        binary_full_path = os.path.join(repo_path, TIDB_BINARY_PATH)
        if not os.path.exists(binary_full_path):
            raise FileNotFoundError(f"编译产物 {binary_full_path} 未找到！")

        tasks[task_id]['log'].append(f"✅ 编译成功: {binary_full_path}")
        return binary_full_path
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        tasks[task_id]['log'].append(f"❌ 在 commit {commit_sha[:8]} 编译失败: {e}")
        return None
    except Exception as e:
        tasks[task_id]['log'].append(f"❌ 发生未知错误在编译时: {e}")
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
        return versions
    except Exception as e:
        print(f"获取 TiDB 版本失败: {e}")
        return ["v8.5.2","v8.1.0", "v8.0.0", "v7.5.1", "v7.1.3", "v6.5.9", "v6.1.7", "v5.4.3", "v4.0.16"]


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
            connection_timeout=20,
            allow_local_infile=True
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
        print(f"SQL 执行失败: {err}")
        return str(err), False


def run_other_check(script_content, port, task_id):
    """执行其他检查脚本"""
    tasks[task_id]['log'].append("--- 开始其他检查 ---")
    log_dir_query = "show config where type='tidb' and name='log.file.filename';"
    try:
        result, success = run_sql_on_tidb(log_dir_query, port)
        if not success or not result:
            msg = "获取 TiDB 日志目录失败。"
            tasks[task_id]['log'].append(f"❌ {msg}")
            return "Failure", msg
        data_list = ast.literal_eval(result)
        log_file_path = data_list[0][3]
        base_dir = os.path.dirname(os.path.dirname(log_file_path))
        tasks[task_id]['log'].append(f"✅ 成功获取到tidb日志目录: {log_file_path}")
        tasks[task_id]['log'].append(f"✅ 脚本将会在此基础目录执行: {base_dir}")
    except Exception as e:
        msg = f"解析 TiDB 日志目录时出错: {e}"
        tasks[task_id]['log'].append(f"❌ {msg}")
        return "Failure", msg

    script_path = os.path.join(base_dir, f"check_script_{task_id[:8]}.sh")
    try:
        with open(script_path, 'w') as f:
            f.write("#!/bin/bash\n")
            f.write(script_content)
        st = os.stat(script_path)
        os.chmod(script_path, st.st_mode | stat.S_IEXEC)
        tasks[task_id]['log'].append(f"✅ 检查脚本已保存到: {script_path}")
        tasks[task_id]['log'].append(f"🚀 执行检查脚本...")
        process = subprocess.run(
            ['/bin/bash', script_path], capture_output=True, text=True, timeout=120, cwd=base_dir
        )
        script_output = process.stdout.strip() + "\n" + process.stderr.strip()
        tasks[task_id]['log'].append(f"脚本输出:\n{script_output}")
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
        if os.path.exists(script_path):
            os.remove(script_path)

def write_search_history(task_id, sql, expected, found_version, found_commit, final_result_msg):
    """
    将定位结果写入 CSV 日志文件
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_exists = os.path.isfile(HISTORY_LOG_FILE)
    
    # 将包含换行符的字段处理一下，避免 CSV 格式混乱 (虽然 CSV 标准支持换行，但单行更易读)
    sql_clean = sql.replace('\n', ' ').strip()
    expected_clean = expected.replace('\n', ' ').strip()
    result_clean = final_result_msg.replace('\n', ' | ').strip()

    try:
        with open(HISTORY_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                # 写入表头
                writer.writerow(['Timestamp', 'Task ID', 'Test Case (SQL)', 'Expected Result', 'Error Version', 'Error Commit', 'Full Result Message'])
            
            writer.writerow([
                timestamp,
                task_id,
                sql_clean,
                expected_clean,
                found_version if found_version else "N/A",
                found_commit if found_commit else "N/A",
                result_clean
            ])
        print(f"✅ 历史记录已写入: {HISTORY_LOG_FILE}")
    except Exception as e:
        print(f"❌ 写入历史记录失败: {e}")

def test_single_version(version, sql, expected_sql_result, other_check_script, task_id, index, cleanup_after=False,
                        commit='', binary_path=None):
    port_offset = random.randint(10000, 30000)
    sql_port = 4000 + port_offset
    dashboard_port = 2379 + port_offset
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_filename = f"{log_dir}/task_{task_id[:8]}_{version}_{commit[:7] if commit else ''}.log"

    log_message = f"版本 {version}" + (f" (commit {commit[:7]})" if commit else "")
    tasks[task_id]['log'].append(f"{log_message}: 准备启动集群 (SQL Port: {sql_port})...")
    result_data = {'version': f"{version}-{commit}" if commit else version}
    process = None
    log_file = None
    startup_success = False
    MAX_STARTUP_RETRIES = 3

    for attempt in range(1, MAX_STARTUP_RETRIES + 1):
        log_file = None
        try:
            # 清理上一次失败的进程
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)

            log_file = open(log_filename, 'w', encoding='utf-8')
            # 如果提供了 binary_path (来自编译)，则使用 --db.binpath 启动
            if commit and binary_path:
                cmd = ['tiup', 'playground', f'--db.binpath={binary_path}', version, f'--port-offset={port_offset}',
                   '--without-monitor', '--kv', str(COMPONENT_COUNTS['tikv']), '--tiflash',
                   str(COMPONENT_COUNTS['tiflash']),
                   '--pd', str(COMPONENT_COUNTS['pd']), '--db', str(COMPONENT_COUNTS['tidb'])]
            else:
                cmd = ['tiup', 'playground', version, f'--port-offset={port_offset}', '--without-monitor',
                   '--kv', str(COMPONENT_COUNTS['tikv']), '--tiflash', str(COMPONENT_COUNTS['tiflash']),
                   '--pd', str(COMPONENT_COUNTS['pd']), '--db', str(COMPONENT_COUNTS['tidb'])]

            print("tiup install cmd:",cmd)
            process = subprocess.Popen(cmd, stdout=log_file, stderr=log_file, text=True, encoding='utf-8')
            if attempt == 1:
                tasks[task_id]['processes'].append(
                    {'version': version, 'process': process, 'offset': port_offset, 'log_file': log_filename})

            tasks[task_id]['log'].append(
            f"{log_message}: 集群启动尝试 {attempt}/{MAX_STARTUP_RETRIES} (PID: {process.pid}, SQL Port: {sql_port})...")

            ready = False
            start_wait_time = time.time()

            # 等待文件有内容写入，稍作缓冲
            time.sleep(1)
            try:
                with open(log_filename, 'r', encoding='utf-8') as reader:
                    while True:
                        # 1. 超时检查 (180秒)
                        if time.time() - start_wait_time > 300:
                            break

                        # 2. 检查进程是否意外退出
                        if process.poll() is not None:
                            # 进程已死，抛出异常以触发重试
                            raise Exception(
                                f"TiUP 进程意外退出 (Return Code: {process.returncode})。请检查日志: {log_filename}")

                        # 3. 读取日志行
                        line = reader.readline()
                        if not line:
                            time.sleep(0.5)  # 文件末尾，等待新日志写入
                            continue

                        # 4. 检查 Ready 信号
                        if "TiDB Playground Cluster is started, enjoy!" in line:
                            ready = True
                            tasks[task_id]['log'].append(f"✅ {log_message}: 检测到 Ready 信号，集群已完全启动。")
                            tasks[task_id]['processes'].append(
                                {'version': version, 'process': process, 'offset': port_offset,
                                 'log_file': log_filename})
                            break
            except FileNotFoundError:
                # 极少数情况下文件可能还没生成
                time.sleep(1)

            if not ready:
                raise Exception("TiDB 服务启动超时 (未检测到 'started' 信号)")

            startup_success = True
            break  # 成功，跳出重试循环

        except Exception as e:
            error_msg = f"❌ 集群启动尝试 {attempt}/{MAX_STARTUP_RETRIES} 失败: {e}"
            tasks[task_id]['log'].append(error_msg)

            # 确保关闭写入句柄
            if log_file and not log_file.closed:
                log_file.close()

            if attempt < MAX_STARTUP_RETRIES:
                time.sleep(5)
            else:  # 所有重试失败
                result_data = {'version': version, 'status': 'Failure',
                               'error': f"集群启动在 {MAX_STARTUP_RETRIES} 次尝试后失败: {e}"}
                tasks[task_id]['results'][index] = result_data
                if process: process.terminate()
                return  # 退出函数

        finally:
            # 确保在函数退出或循环结束时关闭 Popen 使用的写入句柄
            # 注意：如果 process 还在运行，Python 的 Popen 对象会持有这个 fd，
            # 但我们在 Python 侧显式 close 是好习惯，OS 会处理引用计数。
            # 不过在 retry 循环中，需要在 except/finally 里小心处理。
            # 这里我们在循环尾部不强制 close，让 process 持有，
            # 但如果失败重试，上面的 try 开头会 terminate process，那时应该释放。
            pass

    # 循环结束后，保持 process 运行，log_file 句柄由 process 持有 (Python 侧变量可关闭)
    if log_file and not log_file.closed:
        log_file.close()

    try:
        if commit:
            v_result, success = run_sql_on_tidb('select tidb_version();', sql_port)
            if not success or commit not in ''.join(v_result.split()):
                raise Exception(f"TiDB binary 版本不正确! 期望包含 {commit[:10]}, 实际为 {v_result}")
            tasks[task_id]['log'].append("✅ TiDB binary 版本检查通过。")

        # --- 执行检查 ---
        sql_check_passed, other_check_passed = None, None
        final_status = "Success"

        if expected_sql_result is not None:
            actual_sql_result, success = run_sql_on_tidb(sql, sql_port)
            result_data.update({'expected_sql': expected_sql_result, 'actual_sql': actual_sql_result})
            if expected_sql_result.strip():
                if ''.join(expected_sql_result.split()) in ''.join(actual_sql_result.split()):
                    sql_check_passed = True
                else:
                    sql_check_passed = False
            else:
                # if expected is empty，then sql executed success means check pass.
                if success:
                    print("expected sql is empty, and sql executed success")
                    sql_check_passed = True
                else:
                    sql_check_passed = False

        if other_check_script.strip():
            other_status, other_output = run_other_check(other_check_script, sql_port, task_id)
            result_data.update({'other_check_status': other_status, 'other_check_output': other_output})
            other_check_passed = (other_status == "Success")

        if sql_check_passed is False or other_check_passed is False:
            final_status = "Failure"

        result_data.update({'status': final_status, 'sql_port': sql_port, 'dashboard_port': dashboard_port})
    except Exception as e:
        error_msg = f"测试 {log_message} 时发生错误: {e}"
        tasks[task_id]['log'].append(f"❌ {error_msg}")
        result_data = {'version': version, 'status': 'Failure', 'error': str(e)}
    finally:
        if cleanup_after and process:
            tasks[task_id]['log'].append(f"{log_message}: 测试完成，清理集群 (PID: {process.pid})...")
            process.terminate()
            process.wait()

    tasks[task_id]['results'][index] = result_data


# --- 路由 ---
@app.route('/locales/<path:filename>')
def serve_locales(filename):
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
    expected_sql = data.get('expected_sql_result', '').strip()
    other_script = data.get('other_check_script', '').strip()

    COMPONENT_COUNTS = {
        'tidb': int(data.get('tidb') or COMPONENT_COUNTS['tidb']),
        'tikv': int(data.get('tikv') or COMPONENT_COUNTS['tikv']),
        'pd': int(data.get('pd') or COMPONENT_COUNTS['pd']),
        'tiflash': int(data.get('tiflash') or COMPONENT_COUNTS['tiflash'])
    }

    task_id = str(uuid4())
    tasks[task_id] = {'status': 'running', 'log': [], 'results': [{} for _ in selected_versions], 'processes': [],
                      'type': 'test'}
    session.setdefault('task_ids', []).append(task_id)
    session.modified = True

    threads = []
    for i, version in enumerate(selected_versions):
        thread = threading.Thread(target=test_single_version,
                                  args=(version, sql, expected_sql, other_script, task_id, i, False, ''))
        threads.append(thread)
        thread.start()

    def wait_for_completion():
        for t in threads:
            t.join()
        tasks[task_id]['status'] = 'complete'

    threading.Thread(target=wait_for_completion).start()

    return jsonify({'task_id': task_id})


def run_binary_search_with_version(start_v_str, end_v_str, sql, expected_sql, other_check, task_id):
    """二分查找逻辑，现在包含隔离环境的创建和清理"""
    task_repo_path = os.path.join(TIDB_WORKTREE_BASE, task_id)
    # 用于记录最终结果的变量
    found_version = None
    found_commit = None

    try:
        # --- 创建隔离环境 ---
        tasks[task_id]['log'].append(f"为任务 {task_id} 创建隔离的工作目录: {task_repo_path}")
        os.makedirs(TIDB_WORKTREE_BASE, exist_ok=True)
        # 从 end_v_str 推断分支
        version_parts = end_v_str.lstrip('v').split('.')
        branch_version = f"{version_parts[0]}.{version_parts[1]}"
        branch_name = f"release-{branch_version}"
        run_command(["git", "worktree", "add", "-f", task_repo_path, branch_name], work_dir=TIDB_REPO_PATH)
        tasks[task_id]['log'].append(f"✅ Git worktree 创建成功，基于分支 {branch_name}。")

        # --- 内部函数现在使用 repo_path ---
        def commit_binary_search_logic(start_version, end_version, repo_path):
            commits = get_commit_list(start_version, end_version, task_id, repo_path)
            if not commits: return None
            low, high, first_bad_commit = 0, len(commits) - 1, None
            while low <= high:
                mid = (low + high) // 2
                commit_sha = commits[mid]
                tasks[task_id]['log'].append(
                    f"\n--- 正在测试第 {mid + 1}/{len(commits)} 个 commit: {commit_sha[:12]} ---")

                binary_path = compile_at_commit(commit_sha, task_id, end_version, repo_path)
                if binary_path is None:
                    high = mid - 1
                    continue

                result_index = len(tasks[task_id]['results'])
                tasks[task_id]['results'].append({})
                test_single_version(end_version, sql, expected_sql, other_check, task_id, result_index,
                                    cleanup_after=True, commit=commit_sha, binary_path=binary_path)

                result_data = tasks[task_id]['results'][result_index]
                if result_data.get('status') == 'Failure':
                    first_bad_commit = commit_sha
                    high = mid - 1
                elif result_data.get('status') == 'Success':
                    low = mid + 1
                else:
                    tasks[task_id]['log'].append(f"commit {commit_sha[:7]} 测试时发生环境错误，中止。")
                    tasks[task_id]['status'] = 'error'
                    return None
            return first_bad_commit

        # ... (binary_search_logic and baseline checks remain the same, they call test_single_version which doesn't need repo_path)
        all_versions = get_tidb_versions()

        def binary_search_logic(start_version, end_version):
            # This logic doesn't directly interact with git repo, so no repo_path is needed
            search_space = [v for v in all_versions if
                            Version(v) >= Version(start_version) and Version(v) <= Version(end_version)]
            search_space.sort(key=Version)
            low, high, first_bad_version = 0, len(search_space) - 1, None
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
                    tasks[task_id]['log'].append(f"版本 {version_to_test} 测试时发生环境错误，中止。")
                    tasks[task_id]['status'] = 'error'
                    return None
            return first_bad_version

        # --- 执行流程 ---
        # # 1. 基线检查
        tasks[task_id]['log'].append(f"\n--- 正在执行基线检查: {start_v_str} ---")
        start_index = len(tasks[task_id]['results'])
        tasks[task_id]['results'].append({})
        test_single_version(start_v_str, sql, expected_sql, other_check, task_id, start_index, cleanup_after=True)
        start_result = tasks[task_id]['results'][start_index]
        if start_result.get('status') == 'Failure':
            tasks[task_id]['log'].append(f"\n❌ 基线检查失败: 起始版本 {start_v_str} 已不符合预期。")
            tasks[task_id]['final_result'] = "本范围内无法找到引入问题的pr,请在更早的版本或者 commit 范围内查找"
            return
        #
        # # 2. 健全性检查
        tasks[task_id]['log'].append(f"\n--- 正在执行健全性检查: {end_v_str} ---")
        end_index = len(tasks[task_id]['results'])
        tasks[task_id]['results'].append({})
        test_single_version(end_v_str, sql, expected_sql, other_check, task_id, end_index, cleanup_after=True)
        end_result = tasks[task_id]['results'][end_index]
        if end_result.get('status') == 'Success':
            error_msg = f"健全性检查失败: 'Bug 上报版本' ({end_v_str}) 的测试结果为成功，无法进行二分查找。"
            tasks[task_id]['log'].append(f"\n❌ {error_msg}")
            tasks[task_id]['final_result'] = error_msg
            return

        # 3. 开始版本二分查找
        found_version = binary_search_logic(start_v_str, end_v_str)
        if not found_version:
            tasks[task_id]['final_result'] = f"在 {start_v_str}-{end_v_str} 范围内未找到不符合预期的版本。"
            return

        tasks[task_id]['log'].append(f"\n---- 定位到第一个出错的版本是: {found_version} ----")
        tasks[task_id]['final_result'] = f"定位到第一个出错的版本是: {found_version}"

        # 4. 开始 Commit 二分查找
        tidb_versions = get_tidb_versions()
        good_version_index = tidb_versions.index(found_version) + 1
        good_version = tidb_versions[good_version_index]

        found_commit = commit_binary_search_logic(good_version, found_version, task_repo_path)
        if found_commit:
            output = run_command(["git", "show", found_commit, "--no-patch"], work_dir=task_repo_path)

            tasks[task_id][
                'final_result'] = f"定位到第一个出错的commit是: {found_version}-{found_commit}\n\nCommit Info:\n{output}"
        else:
            tasks[task_id]['final_result'] += f"\n但在 {good_version} 和 {found_version} 之间未定位到具体的 commit。"

    except Exception as e:
        tasks[task_id]['log'].append(f"❌ 二分查找过程中发生严重错误: {e}")
        tasks[task_id]['status'] = 'error'
    finally:
        try:
            write_search_history(
                task_id,
                sql,
                expected_sql,
                found_version,
                found_commit,
                tasks[task_id].get('final_result', 'N/A')
            )
        except Exception:
            pass
        # --- 清理隔离环境 ---
        if os.path.exists(task_repo_path):
            tasks[task_id]['log'].append(f"清理任务 {task_id} 的工作目录: {task_repo_path}")
            try:
                # 使用 git worktree remove 更干净
                run_command(["git", "worktree", "remove", "--force", task_repo_path], work_dir=TIDB_REPO_PATH)
            except Exception as e:
                tasks[task_id]['log'].append(f"⚠️ Git worktree remove 失败: {e}. 尝试手动删除目录...")
                shutil.rmtree(task_repo_path, ignore_errors=True)
        tasks[task_id]['status'] = 'complete'


def run_binary_search_with_commit(start_commit, end_commit, branch, sql, expected_sql, other_check, task_id):
    """二分查找逻辑，现在包含隔离环境的创建和清理"""
    task_repo_path = os.path.join(TIDB_WORKTREE_BASE, task_id)
    # 用于记录最终结果的变量
    found_commit = None

    try:
        # --- 创建隔离环境 ---
        tasks[task_id]['log'].append(f"为任务 {task_id} 创建隔离的工作目录: {task_repo_path}")
        os.makedirs(TIDB_WORKTREE_BASE, exist_ok=True)
        run_command(["git", "worktree", "add", "-f", task_repo_path, branch], work_dir=TIDB_REPO_PATH)
        tasks[task_id]['log'].append(f"✅ Git worktree 创建成功，基于分支 {branch}。")

        tasks[task_id]['log'].append(f"🔄 正在更新仓库代码...")
        run_command(["git", "pull"], work_dir=task_repo_path, print_output=True)
        tasks[task_id]['log'].append(f"✅ 仓库代码已更新。")

        # --- 内部函数 ---
        def commit_binary_search_logic(repo_path):
            command = ["git", "rev-list", "--reverse", f"{start_commit}..{end_commit}"]
            result = run_command(command, work_dir=repo_path)
            commits_after_start = [line for line in result.strip().split('\n') if line]
            commits = [start_commit] + commits_after_start

            low, high, first_bad_commit = 0, len(commits) - 1, None

            while low <= high:
                mid = (low + high) // 2
                commit_sha = commits[mid]
                tasks[task_id]['log'].append(
                    f"\n--- 正在测试第 {mid + 1}/{len(commits)} 个 commit: {commit_sha[:12]} ---")

                install_version = 'nightly' if branch == 'master' else f'v{branch.replace("release-", "")}.0'
                binary_path = compile_at_commit(commit_sha, task_id, install_version, repo_path)
                if binary_path is None:
                    high = mid - 1
                    continue

                result_index = len(tasks[task_id]['results'])
                tasks[task_id]['results'].append({})
                test_single_version(install_version, sql, expected_sql, other_check, task_id, result_index,
                                    cleanup_after=True, commit=commit_sha, binary_path=binary_path)

                result_data = tasks[task_id]['results'][result_index]
                if result_data.get('status') == 'Failure':
                    first_bad_commit = commit_sha
                    high = mid - 1
                elif result_data.get('status') == 'Success':
                    low = mid + 1
                else:
                    tasks[task_id]['log'].append(f"commit {commit_sha[:7]} 测试时发生环境错误，中止。")
                    tasks[task_id]['status'] = 'error'
                    return None
            return first_bad_commit

        def test_a_commit(commit_sha, index, repo_path):
            install_version = 'nightly' if branch == 'master' else f'v{branch.replace("release-", "")}.0'
            binary_path = compile_at_commit(commit_sha, task_id, install_version, repo_path)
            if binary_path is None:
                tasks[task_id]['results'][index] = {'version': commit_sha, 'status': 'Failure', 'error': '编译失败'}
                return
            test_single_version(install_version, sql, expected_sql, other_check, task_id, index, cleanup_after=True,
                                commit=commit_sha, binary_path=binary_path)

        # --- 执行流程 ---
        # 1. 基线检查
        tasks[task_id]['log'].append(f"\n--- 正在执行基线检查 (起始 Commit): {start_commit[:7]} ---")
        start_index = len(tasks[task_id]['results'])
        tasks[task_id]['results'].append({})
        test_a_commit(start_commit, start_index, task_repo_path)

        start_result = tasks[task_id]['results'][start_index]
        if start_result.get('status') == 'Failure':
            tasks[task_id]['log'].append(f"\n❌ 基线检查失败: 起始 Commit {start_commit[:7]} 已不符合预期。")
            tasks[task_id]['final_result'] = "本范围内无法找到引入问题的pr,请在更早的版本或者commit 范围内查找"
            return

        # 2. 检查结束commit
        tasks[task_id]['log'].append(f"\n--- 正在执行健全性检查: {end_commit[:7]} ---")
        end_index = len(tasks[task_id]['results'])
        tasks[task_id]['results'].append({})
        test_a_commit(end_commit, end_index, task_repo_path)

        end_result = tasks[task_id]['results'][end_index]
        if end_result.get('status') == 'Success':
            error_msg = f"健全性检查失败: 'Bug 上报commit' ({end_commit}) 的测试结果为成功，无法进行二分查找。"
            tasks[task_id]['log'].append(f"\n❌ {error_msg}")
            tasks[task_id]['final_result'] = error_msg
            return

        # 2. 开始二分查找
        found_commit = commit_binary_search_logic(task_repo_path)
        if found_commit:
            output = run_command(["git", "show", found_commit, "--no-patch"], work_dir=task_repo_path)
            tasks[task_id]['final_result'] = f"定位到第一个出错的commit是: {found_commit}\n\nCommit Info:\n{output}"
        else:
            tasks[task_id][
                'final_result'] = f"在 {branch} 分支的 {start_commit[:7]}..{end_commit[:7]} 范围内未找到不符合预期的commit。"

    except Exception as e:
        tasks[task_id]['log'].append(f"❌ 二分查找过程中发生严重错误: {e}")
        tasks[task_id]['status'] = 'error'
    finally:
        # --- 记录历史日志 ---
        try:
            write_search_history(
                task_id, 
                sql, 
                expected_sql, 
                "Commit Mode", # 版本模式下没有版本号
                found_commit, 
                tasks[task_id].get('final_result', 'N/A')
            )
        except Exception:
            pass

        # --- 清理隔离环境 ---
        if os.path.exists(task_repo_path):
            tasks[task_id]['log'].append(f"清理任务 {task_id} 的工作目录: {task_repo_path}")
            try:
                run_command(["git", "worktree", "remove", "--force", task_repo_path], work_dir=TIDB_REPO_PATH)
            except Exception as e:
                tasks[task_id]['log'].append(f"⚠️ Git worktree remove 失败: {e}. 尝试手动删除目录...")
                shutil.rmtree(task_repo_path, ignore_errors=True)
        tasks[task_id]['status'] = 'complete'


@app.route('/start_locate', methods=['POST'])
def start_locate():
    global COMPONENT_COUNTS
    data = request.json
    locate_mode = data.get('locate_mode')
    sql = data.get('sql')
    expected_sql_result = data.get('expected_sql_result', '').strip()
    other_check_script = data.get('other_check_script', '').strip()

    COMPONENT_COUNTS = {
        'tidb': int(data.get('tidb') or COMPONENT_COUNTS['tidb']),
        'tikv': int(data.get('tikv') or COMPONENT_COUNTS['tikv']),
        'pd': int(data.get('pd') or COMPONENT_COUNTS['pd']),
        'tiflash': int(data.get('tiflash') or COMPONENT_COUNTS['tiflash'])
    }

    task_id = str(uuid4())
    tasks[task_id] = {'status': 'running', 'log': [], 'results': [], 'processes': [], 'type': 'locate'}
    session.setdefault('task_ids', []).append(task_id)
    session.modified = True

    if locate_mode == 'version':
        bug_version = data.get('bug_version')
        start_version_str = data.get('start_version') or "v5.4.0"
        if not bug_version or Version(start_version_str) >= Version(bug_version):
            return jsonify({'error': '版本设置无效：“起始版本”必须早于“Bug 上报版本”'}), 400
        thread = threading.Thread(target=run_binary_search_with_version,
                                  args=(start_version_str, bug_version, sql, expected_sql_result, other_check_script,
                                        task_id))
    elif locate_mode == 'commit':
        branch = data.get('branch')
        start_commit = data.get('start_commit')
        end_commit = data.get('end_commit')
        if not all([branch, start_commit, end_commit]):
            return jsonify({'error': '分支、起始 Commit 和结束 Commit 均为必填项'}), 400
        thread = threading.Thread(target=run_binary_search_with_commit,
                                  args=(start_commit, end_commit, branch, sql, expected_sql_result, other_check_script,
                                        task_id))
    else:
        return jsonify({'error': f'未知的定位模式: {locate_mode}'}), 400

    thread.start()
    return jsonify({'task_id': task_id})


@app.route('/status/<task_id>')
def task_status(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({'status': 'not_found'}), 404

    serializable_task = {
        'status': task.get('status'),
        'log': task.get('log', []),
        'results': task.get('results', []),
        'type': task.get('type'),
        'final_result': task.get('final_result'),
    }
    return jsonify(serializable_task)


@app.route('/clean', methods=['POST'])
def clean_env():
    """清理当前 session 创建的所有 tiup playground 进程和日志文件"""
    task_ids_to_clean = session.get('task_ids', [])
    cleaned_pids, deleted_logs, errors = [], [], []

    for task_id in task_ids_to_clean:
        task = tasks.get(task_id)
        if not task or not task.get('processes'):
            continue

        for proc_info in task['processes']:
            process = proc_info.get('process')
            if process and process.poll() is None:
                try:
                    pid = process.pid
                    process.terminate()
                    process.wait(timeout=30)
                    cleaned_pids.append(pid)
                except Exception as e:
                    errors.append(f"清理进程 PID {pid} 失败: {e}")

            log_file = proc_info.get('log_file')
            if log_file and os.path.exists(log_file):
                try:
                    os.remove(log_file)
                    deleted_logs.append(log_file)
                except OSError as e:
                    errors.append(f"删除日志文件 {log_file} 失败: {e}")

    session['task_ids'] = []
    session.modified = True

    return jsonify({
        'message': '清理完成。注意: 手动创建的编译目录 (如 /tmp/tidb_worktrees) 在异常退出时可能需要手动清理。',
        'cleaned_pids': cleaned_pids,
        'deleted_logs': deleted_logs,
        'errors': errors
    })


if __name__ == '__main__':
    # 确保 worktree 基准目录存在
    os.makedirs(TIDB_WORKTREE_BASE, exist_ok=True)
    app.run(debug=True, host='0.0.0.0', port=5001)

