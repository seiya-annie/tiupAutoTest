import os
import subprocess
import docker
import random
import time
import json
import threading
from uuid import uuid4
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from packaging.version import Version
import mysql.connector

# --- 配置 ---
app = Flask(__name__)
# 用于 session 加密，请在生产环境中替换为更复杂的密钥
app.secret_key = 'a_very_secret_key_for_tidb_tester'

# Docker 客户端
try:
    docker_client = docker.from_env()
    docker_client.ping()
except Exception as e:
    print(f"无法连接到 Docker. 请确保 Docker 正在运行: {e}")
    docker_client = None

# 用于存储后台任务的状态和结果
tasks = {}


# --- 辅助函数 ---

def get_tidb_versions():
    """通过 tiup list tidb 获取可用的 TiDB 版本列表"""
    try:
        # 确保 tiup 组件是最新的
        subprocess.run(["tiup", "update", "--self"], check=True, capture_output=True, text=True)
        result = subprocess.run(
            ["tiup", "list", "tidb"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60
        )
        versions = []
        for line in result.stdout.splitlines():
            # 过滤出稳定版本，例如 v6.1.0, v5.4.3
            if line.strip().startswith('v') and 'Available versions' not in line and '---' not in line:
                # 只取版本号部分
                version = line.split()[0]
                if all(c in 'v0123456789.' for c in version):
                    versions.append(version)
        # 按版本号降序排序
        versions.sort(key=Version, reverse=True)
        return versions
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"获取 TiDB 版本失败: {e}")
        # 如果 tiup 失败，提供一些默认值
        return ["v7.1.0", "v7.0.0", "v6.5.0", "v6.1.0", "v5.4.0", "v4.0.8"]


def find_free_port():
    """查找一个未被占用的端口"""
    return random.randint(10000, 20000)


def run_sql_on_tidb(sql, port):
    """在指定的 TiDB 实例上执行 SQL"""
    result = ""
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
        # 支持多条 SQL 语句
        for stmt in sql.split(';'):
            if stmt.strip():
                cursor.execute(stmt)
                if cursor.with_rows:
                    rows = cursor.fetchall()
                    result += str(rows) + "\n"
        conn.commit()
        cursor.close()
        conn.close()
        return result.strip(), True
    except mysql.connector.Error as err:
        return f"SQL 执行错误: {err}", False


def test_single_version(version, sql, expected_result, task_id, index):
    """启动一个 TiDB 容器并执行测试 (最终修正版)"""
    if not docker_client:
        tasks[task_id]['results'][index] = {'version': version, 'status': '失败', 'error': 'Docker 未连接'}
        return

    sql_port = find_free_port()
    dashboard_port = find_free_port()
    container_name = f"tidb-test-{version}-{task_id[:8]}"

    log_message = f"版本 {version}: 准备启动容器 {container_name} (SQL Port: {sql_port}, Dashboard: {dashboard_port})..."
    tasks[task_id]['log'].append(log_message)
    print(log_message)

    container = None
    result_data = {}
    try:
        image_name = f'hub.pingcap.net/qa/tidb-playground:latest'
        container = docker_client.containers.run(
            image_name,
            ["--db.host", "0.0.0.0", "--tiflash", "0", "--db.config", "/root/.tiup/config.toml", f"{version}"],
            detach=True,
            ports={'4000/tcp': sql_port, '2379/tcp': dashboard_port},
            remove=True  # 设置 docker 在容器停止时自动删除
        )

        log_message = f"版本 {version}: 容器 {container.short_id} 已启动，等待 TiDB 服务就绪..."
        tasks[task_id]['log'].append(log_message)
        print(log_message)

        ready = False
        for _ in range(24):
            time.sleep(10)
            try:
                conn = mysql.connector.connect(host='127.0.0.1', port=sql_port, user='root', password='',
                                               connection_timeout=5)
                conn.close()
                ready = True
                log_message = f"版本 {version}: TiDB 服务在端口 {sql_port} 上已就绪。"
                tasks[task_id]['log'].append(log_message)
                print(log_message)
                break
            except mysql.connector.Error:
                continue

        if not ready:
            raise Exception("TiDB 服务启动超时")

        actual_result, success = run_sql_on_tidb(sql, sql_port)

        if not success:
            status = "执行失败"
        elif ''.join(expected_result.split()) in ''.join(actual_result.split()):
            status = "成功"
        else:
            status = "失败"

        result_data = {
            'version': version,
            'status': status,
            'sql_port': sql_port,
            'dashboard_port': dashboard_port,
            'expected': expected_result,
            'actual': actual_result,
        }

    except docker.errors.ImageNotFound as e:
        error_msg = f"Docker image tidb-playground:latest 不存在。"
        tasks[task_id]['log'].append(f"版本 {version}: {error_msg}")
        result_data = {'version': version, 'status': '失败', 'error': error_msg}
    except Exception as e:
        error_msg = f"测试版本 {version} 时发生未知错误: {e}"
        tasks[task_id]['log'].append(error_msg)
        result_data = {'version': version, 'status': '失败', 'error': str(e)}
    finally:
        if container:
            result_data['container_id'] = container.id
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
    """主页测试入口 (最终修正版)"""
    data = request.json
    selected_versions = data.get('versions', [])
    sql = data.get('sql')
    expected_result = data.get('expected')

    task_id = str(uuid4())
    tasks[task_id] = {
        'status': 'running',
        'log': [],
        'results': [{} for _ in selected_versions],
        'type': 'test'
    }

    # 在主请求线程中，将 task_id 与当前用户的 session 关联
    session.setdefault('task_ids', []).append(task_id)
    session.modified = True

    threads = []
    for i, version in enumerate(selected_versions):
        thread = threading.Thread(target=test_single_version, args=(version, sql, expected_result, task_id, i))
        threads.append(thread)
        thread.start()

    def wait_for_completion():
        for t in threads:
            t.join()
        tasks[task_id]['status'] = 'complete'

    threading.Thread(target=wait_for_completion).start()

    return jsonify({'task_id': task_id})


@app.route('/start_locate', methods=['POST'])
def start_locate():
    """自动定位入口 (最终修正版)"""
    data = request.json
    bug_version = data.get('bug_version')
    start_version_str = data.get('start_version') or "v5.4.0"
    print("start version:", start_version_str)
    sql = data.get('sql')
    expected_result = data.get('expected_result')

    if not bug_version:
        return jsonify({'error': '“bug 上报版本”不能为空'}), 400
    try:
        if Version(start_version_str) >= Version(bug_version):
            return jsonify({'error': '“起始版本”不能等于或者晚于“bug 上报版本”'}), 400
    except Exception:
        return jsonify({'error': '版本号格式无效'}), 400

    task_id = str(uuid4())
    tasks[task_id] = {
        'status': 'running',
        'log': [],
        'results': [],
        'type': 'locate'
    }

    # 【重要】在这里同样需要将 task_id 与当前用户的 session 关联
    session.setdefault('task_ids', []).append(task_id)
    session.modified = True

    thread = threading.Thread(target=run_binary_search,
                              args=(start_version_str, bug_version, sql, expected_result, task_id))
    thread.start()

    return jsonify({'task_id': task_id})


def run_binary_search(start_v_str, end_v_str, sql, expected, task_id):
    """二分查找逻辑 (最终修正版)"""
    all_versions = get_tidb_versions()

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

            # 【修复】获取下一个可用的索引号
            result_index = len(tasks[task_id]['results'])
            # 【修复】在列表中添加一个占位符，以使索引有效
            tasks[task_id]['results'].append({})

            test_single_version(version_to_test, sql, expected, task_id, result_index)

            # 延迟一小会儿，确保前端能刷新到日志
            time.sleep(0.1)
            result_data = tasks[task_id]['results'][result_index]

            if result_data.get('status') == '失败' and 'error' not in result_data:
                first_bad_version = version_to_test
                high = mid_idx - 1
            elif result_data.get('status') == '成功':
                low = mid_idx + 1
            else:
                tasks[task_id]['log'].append(f"版本 {version_to_test} 测试时发生环境错误，定位中止。")
                tasks[task_id]['status'] = 'error'
                return None

        return first_bad_version

    # --- 主逻辑 ---
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

    tasks[task_id]['status'] = 'complete'


@app.route('/status/<task_id>')
def task_status(task_id):
    """获取任务状态"""
    task = tasks.get(task_id)
    if not task:
        return jsonify({'status': 'not_found'}), 404
    return jsonify(task)


@app.route('/clean', methods=['POST'])
def clean_env():
    """清理当前 session 创建的所有任务的容器 (最终修正版)"""
    if not docker_client:
        return jsonify({'error': 'Docker 未连接'}), 500

    task_ids_to_clean = session.get('task_ids', [])
    if not task_ids_to_clean:
        return jsonify({'cleaned': [], 'errors': [], 'message': '当前会话没有需要清理的任务。'})

    cleaned_ids = []
    errors = []

    for task_id in task_ids_to_clean:
        task = tasks.get(task_id)
        if not task or not task.get('results'):
            continue

        for result in task['results']:
            # 确保 result 是一个字典并且包含 'container_id'
            if isinstance(result, dict) and 'container_id' in result:
                c_id = result.get('container_id')
                if not c_id:
                    continue

                try:
                    container = docker_client.containers.get(c_id)
                    container.stop(timeout=10)
                    cleaned_ids.append(container.short_id)
                except docker.errors.NotFound:
                    pass
                except Exception as e:
                    errors.append(f"删除容器 {c_id[:12]} 失败: {e}")

    session['task_ids'] = []
    session.modified = True
    return jsonify({'cleaned': cleaned_ids, 'errors': errors})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
