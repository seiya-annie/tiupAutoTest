document.addEventListener('DOMContentLoaded', function() {
    const resultBox = document.getElementById('results');
    let pollInterval;

    // --- 通用函数 ---
    function pollStatus(taskId) {
        if (pollInterval) {
            clearInterval(pollInterval);
        }

        pollInterval = setInterval(async () => {
            try {
                const response = await fetch(`/status/${taskId}`);
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                const data = await response.json();

                updateResults(data);

                if (data.status === 'complete' || data.status === 'error' || data.status === 'not_found') {
                    clearInterval(pollInterval);
                    document.querySelectorAll('button').forEach(b => b.disabled = false);
                }
            } catch (error) {
                console.error('Error polling for status:', error);
                resultBox.innerHTML += `\n polling failed: ${error.message}`;
                clearInterval(pollInterval);
                document.querySelectorAll('button').forEach(b => b.disabled = false);
            }
        }, 3000); // 每 3 秒轮询一次
    }

    function updateResults(data) {
        let content = `<strong>任务状态: ${data.status}</strong>\n\n`;
        content += "<strong>日志:</strong>\n" + data.log.join('\n') + '\n\n';

        if (data.results && data.results.length > 0) {
             content += "<strong>测试详情:</strong>\n";
             data.results.forEach(res => {
                 if (Object.keys(res).length === 0) return; // 跳过占位符
                 content += `---------------------------------\n`;
                 content += `版本: ${res.version}\n`;
                 content += `状态: <span class="${res.status === '成功' ? 'success' : 'failure'}">${res.status}</span>\n`;
                 if (res.error) {
                     content += `错误: ${res.error}\n`;
                 } else {
                     content += `SQL Port: ${res.sql_port}, Dashboard Port: ${res.dashboard_port}\n`;
                     content += `预期: ${res.expected}\n`;
                     content += `实际: ${res.actual}\n`;
                 }
             });
        }

        if (data.type === 'test' && data.status === 'complete') {
            const successVersions = data.results.filter(r => r.status === '成功').map(r => r.version);
            const failedVersions = data.results.filter(r => r.status !== '成功').map(r => r.version);
            content += "\n\n<strong>--- 总结 ---</strong>\n";
            content += `成功版本: ${successVersions.join(', ') || '无'}\n`;
            content += `失败版本: ${failedVersions.join(', ') || '无'}\n`;
        }

        if (data.type === 'locate' && data.status === 'complete' && data.final_result) {
            content += `\n\n<strong>--- 最终定位结果 ---</strong>\n<strong class="failure">${data.final_result}</strong>\n`;
        }

        resultBox.textContent = content;
    }

    async function cleanEnvironment(btn) {
        btn.disabled = true;
        btn.textContent = '清理中...';
        resultBox.textContent += '\n\n开始清理环境...';
        try {
            const response = await fetch('/clean', { method: 'POST' });
            const data = await response.json();
            let msg = `\n清理完成。 已清理 ${data.cleaned.length} 个容器。`;
            if (data.errors.length > 0) {
                msg += `\n错误: ${data.errors.join(', ')}`;
            }
            resultBox.textContent += msg;
        } catch (error) {
            resultBox.textContent += `\n清理失败: ${error}`;
        } finally {
            btn.disabled = false;
            if(btn.id === 'clean-env-btn') btn.textContent = '清理环境';
            else btn.textContent = '6. 清理环境';
        }
    }

    // --- 首页逻辑 (index.html) ---
    const startTestBtn = document.getElementById('start-test-btn');
    if (startTestBtn) {
        startTestBtn.addEventListener('click', async () => {
            const selectedOptions = document.getElementById('tidb-versions').selectedOptions;
            const versions = Array.from(selectedOptions).map(el => el.value);
            const sql = document.getElementById('sql-query').value;
            const expected = document.getElementById('expected-result').value;

            if (versions.length === 0) {
                alert('请至少选择一个 TiDB 版本');
                return;
            }

            document.querySelectorAll('button').forEach(b => b.disabled = true);
            resultBox.textContent = '任务已提交，正在初始化...';

            const response = await fetch('/start_test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ versions, sql, expected })
            });

            const data = await response.json();
            if (data.task_id) {
                pollStatus(data.task_id);
            } else {
                 resultBox.textContent = `启动失败: ${data.error}`;
                 document.querySelectorAll('button').forEach(b => b.disabled = false);
            }
        });
    }

    const autoLocateBtn = document.getElementById('auto-locate-btn');
    if(autoLocateBtn) {
        autoLocateBtn.addEventListener('click', () => {
            // 将主界面的值存入 localStorage，以便子界面读取
            localStorage.setItem('sqlQuery', document.getElementById('sql-query').value);
            localStorage.setItem('expectedResult', document.getElementById('expected-result').value);
            window.location.href = '/locate';
        });
    }

    // --- 定位页逻辑 (locate.html) ---
    if (window.location.pathname === '/locate') {
        const sqlQueryFromHome = localStorage.getItem('sqlQuery');
        const expectedResultFromHome = localStorage.getItem('expectedResult');

        const sqlTextarea = document.getElementById('sql-query');
        const expectedTextarea = document.getElementById('expected-result');

        // 如果不是默认值，则从首页拷贝
        if (sqlQueryFromHome && sqlQueryFromHome.trim() !== 'SELECT 1;') {
            sqlTextarea.value = sqlQueryFromHome;
        }
        if (expectedResultFromHome && expectedResultFromHome.trim() !== '[(1,)]') {
            expectedTextarea.value = expectedResultFromHome;
        }
    }

    const startLocateBtn = document.getElementById('start-locate-btn');
    if (startLocateBtn) {
        startLocateBtn.addEventListener('click', async () => {
            const bug_version = document.getElementById('bug-version').value;
            const start_version = document.getElementById('start-version').value;
            const sql = document.getElementById('sql-query').value;
            const expected = document.getElementById('expected-result').value;

            if (!bug_version) {
                alert('请填写“Bug 上报版本”');
                return;
            }

            document.querySelectorAll('button').forEach(b => b.disabled = true);
            resultBox.textContent = '定位任务已提交，正在初始化...';

            const response = await fetch('/start_locate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ bug_version, start_version, sql, expected_result: expected })
            });

            const data = await response.json();
            if (data.error) {
                resultBox.textContent = `启动失败: ${data.error}`;
                document.querySelectorAll('button').forEach(b => b.disabled = false);
            } else if (data.task_id) {
                pollStatus(data.task_id);
            }
        });
    }

    // --- 清理按钮通用逻辑 ---
    document.querySelectorAll('#clean-env-btn').forEach(btn => {
        btn.addEventListener('click', () => cleanEnvironment(btn));
    });
});