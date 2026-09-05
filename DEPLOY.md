# 云服务器部署步骤（单店版）

系统：Flask + SQLite，无需 MySQL/Redis，单文件数据库。

## 1. 服务器准备（阿里云/腾讯云，Ubuntu 22.04，2核2G 起步）

```bash
apt update && apt install -y python3 python3-pip python3-venv
useradd -m ysg          # 不用 root 跑
su - ysg
git clone https://github.com/wcatm/ysg.git
cd ysg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 2. 上传数据（本机已有的会员/订单数据）

把本机 `ysg.db` 拷到服务器项目目录（覆盖空库即可，首启会自动初始化）：

```bash
scp ysg.db root@服务器IP:/home/ysg/ysg/
chown ysg:ysg /home/ysg/ysg/ysg.db
```

首次启动会打印初始 admin 账号密码（仅首次），记下来登录后修改。

## 3. 启动（waitress 生产服务器，80 端口）

```bash
# 开机自启：写入 systemd
sudo tee /etc/systemd/system/ysg.service <<'EOF'
[Unit]
Description=ysg
After=network.target

[Service]
User=ysg
WorkingDirectory=/home/ysg/ysg
ExecStart=/home/ysg/ysg/.venv/bin/python -c "from waitress import serve; import app; serve(app.app, host='0.0.0.0', port=80)"
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now ysg
```

浏览器访问 `http://服务器IP/admin/login` 即后台。

## 4. 备案通过后接域名

1. 域名解析 A 记录 → 服务器 IP
2. 站名可在后台「门店配置」改（site_name）
3. 若需 HTTPS：`apt install -y nginx certbot python3-certbot-nginx`，nginx 反代 80 后 certbot 自动签证书

## 5. 日常维护

- 备份数据库：`cp ysg.db ysg_backup_$(date +%F).db`（建议 crontab 每日）
- 改代码后：`sudo systemctl restart ysg`
- 日志：`journalctl -u ysg -f`
- 自检：`python app.py --selftest`
