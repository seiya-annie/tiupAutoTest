# TiDB Bug Locator & Regression Test Platform
This tool helps developers and testers to quickly locate bugs in TiDB and run regression tests across multiple versions.

本工具旨在帮助开发者和测试人员快速定位 TiDB 代码中的 Bug，并进行多版本回归测试。它通过一个直观的 Web 界面，自动化地调用 tiup playground 来完成集群的创建、测试和销毁。

## ✨ Features (主要功能)
* Multi-Version Testing (多版本测试): Concurrently run regression tests against multiple TiDB versions by launching isolated tiup playground clusters.

* Automatic Bug Bisection (自动定位 Bug): Given a known faulty version, the tool uses a binary search algorithm to automatically pinpoint the exact version that introduced the bug.

## 🚀 Getting Started
### Prerequisites (准备工作)
Before you begin, ensure you have the following installed:

* Python 3.8+

* Python Dependencies: From the project root directory, run:

```pip install -r requirements.txt```
* install go versions with asdf tool
```  
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
```
### Running the Application (运行代码)
You can start the web service in one of two ways:

#### Option 1: Direct Execution

```python app.py```

#### Option 2: Using the Flask Command

Set the FLASK_APP environment variable first
```export FLASK_APP=app.py```

Then, run the application
```flask run --host=0.0.0.0```

# 💻 Usage (使用方法)
Once the server is running, open your web browser and navigate to:

http://127.0.0.1:5001

From the homepage, you can select one or more TiDB versions for regression testing.

Enter your SQL query and the expected result.

Click "Start Test" to begin.

To find a specific bug, click "Auto Locate Bug" to navigate to the bisection page.

In Other Checks(Shell script) input-box, you can input the shell script code to check tidb log and so on. eg.
```
KEYWORD="panic"
PANIC_FILES=$(find . -name "tidb.log" -type f -print0 | xargs -0 grep -li "$KEYWORD")
if [ -z "$PANIC_FILES" ]; then
  echo "检查通过: 在任何 tidb.log 文件中均未发现 'panic' 字符串。"
  exit 0 # 成功退出
else
  echo "检查失败: 在以下文件中发现了 'panic' 字符串:"
  echo "$PANIC_FILES"
  exit 1 # 失败退出
fi
```

Always use the "Clean Environment" button after your tests to terminate all running tiup processes and remove log files for your session.

# 🔧 (Optional) Using a Custom Docker Image
If you can not pull the image in app.py, you can self-compiled a TiDB tiup playground running image, you can use the provided Dockerfile to build a custom image. After building, you will need to modify the app.py script to use your new image name.

# 🤝 Contributing
Contributions are welcome! Please feel free to submit a Pull Request or open an issue for any bugs or feature requests.


