# 使用 bitnami 的最小化 Debian 作为基础镜像
FROM bitnami/minideb:latest

# 安装 curl 工具并清理 apt 缓存
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# 下载并安装 tiup，并创建软链接以便全局调用
# 注意：这里安装的是 tiup 工具本身
RUN curl --proto '=https' --tlsv1.2 -sSf https://tiup-mirrors.pingcap.com/install.sh | sh \
    && ln -s /root/.tiup/bin/tiup /usr/local/bin/tiup

# 安装 playground 组件，以便后续可以启动任何版本的集群
# 我们不再在构建时安装特定版本的 tidb/pd/tikv
RUN tiup install playground

# 将本地的 config.toml 配置文件复制到镜像中 tiup 的配置目录下
COPY config.toml /root/.tiup/config.toml

# 暴露 playground 默认的 SQL 客户端端口
EXPOSE 4000
# 暴露 playground 默认的 Dashboard 端口
EXPOSE 2379

# 配置容器的入口点 (Entrypoint)
# 这是容器启动时要执行的固定命令部分
ENTRYPOINT ["tiup", "playground"]

# 配置默认命令 (CMD)，它会作为参数附加到 ENTRYPOINT 后面
# 用户在 docker run 时可以轻松覆盖这个默认版本号
CMD ["--db.host", "0.0.0.0", "--tiflash", "0", "--db.config", "/root/.tiup/config.toml", "latest"]