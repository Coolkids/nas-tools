import base64
import datetime
import hashlib
import os.path
import re
import shutil
import sqlite3
import time
import traceback
import urllib
import xml.dom.minidom
from functools import wraps
from math import floor
from pathlib import Path
from threading import Lock
from urllib import parse

from flask import Flask, request, json, make_response, session, send_from_directory, send_file, Response
from flask_compress import Compress
from flask_login import LoginManager, login_user, login_required, current_user, logout_user

import log
from app.conf import ModuleConf
from app.db import close_db
from app.helper import SecurityHelper, ThreadHelper
from app.media.meta import MetaInfo
from app.mediaserver import WebhookEvent
from app.message import Message
from app.speedlimiter import SpeedLimiter
from app.subscribe import Subscribe
from app.utils import DomUtils, SystemUtils, ExceptionUtils, StringUtils
from app.utils.types import *
from config import PT_TRANSFER_INTERVAL, Config
from web.action import WebAction
from web.apiv1 import apiv1_bp
from web.backend.WXBizMsgCrypt3 import WXBizMsgCrypt
from web.backend.user import User
from web.backend.wallpaper import get_login_wallpaper
from web.backend.web_utils import WebUtils
from web.security import require_auth

# 配置文件锁
ConfigLock = Lock()

# Flask App
App = Flask(__name__)
App.config['JSON_AS_ASCII'] = False
App.secret_key = os.urandom(24)
App.permanent_session_lifetime = datetime.timedelta(days=30)

# 启用压缩
Compress(App)

# 登录管理模块
LoginManager = LoginManager()
LoginManager.login_view = "login"
LoginManager.init_app(App)

# API注册
App.register_blueprint(apiv1_bp, url_prefix="/api/v1")


@App.after_request
def add_header(r):
    """
    统一添加Http头，标用缓存，避免Flask多线程+Chrome内核会发生的静态资源加载出错的问题
    r.headers["Cache-Control"] = "no-cache, no-store, max-age=0"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    """
    return r


@App.teardown_request
def shutdown_session(exception=None):
    """
    请求结束时释放 scoped_session，归还连接到连接池，
    防止连接泄漏导致 QueuePool 耗尽 (size 10 + overflow 100)
    """
    close_db()


# 定义获取登录用户的方法
@LoginManager.user_loader
def load_user(user_id):
    return User().get(user_id)


# 页面不存在
@App.errorhandler(404)
def page_not_found(error):
    return 404


# 服务错误
@App.errorhandler(500)
def page_server_error(error):
    return 500


def action_login_check(func):
    """
    Action安全认证
    """

    @wraps(func)
    def login_check(*args, **kwargs):
        if not current_user.is_authenticated:
            return {"code": -1, "msg": "用户未登录"}
        return func(*args, **kwargs)

    return login_check



# 主页面 - SPA 前端已分离至 nas-tools-web，后端仅提供 JSON API
@App.route("/", methods=["GET"])
def index_root():
    return {"code": 0, "message": "NAStool API is running. Frontend should be served separately."}


@App.route("/index", methods=["GET"])
def index_health():
    return {"code": 0, "message": "ok"}



# 壁纸接口（供 Vue 前端使用，无登录要求）
@App.route('/wallpaper', methods=['GET'])
def get_wallpaper():
    from web.backend.wallpaper import get_login_wallpaper
    return {"code": 0, "wallpaper": get_login_wallpaper()}


# JSON 登录接口（供 Vue 前端使用，前后端分离）
@App.route('/login_json', methods=['POST'])
def login_json():
    username = request.form.get('username')
    password = request.form.get('password')
    remember = request.form.get('remember')
    if not username or not password:
        return {"code": 1, "success": False, "message": "请输入用户名和密码"}
    user_info = User().get_user(username)
    if not user_info or not user_info.verify_password(password):
        return {"code": 1, "success": False, "message": "用户名或密码错误"}
    login_user(user_info)
    session.permanent = True if remember else False
    return {
        "code": 0,
        "success": True,
        "message": "登录成功",
        "data": {
            "userid": user_info.id,
            "username": user_info.username,
            "userpris": str(user_info.pris).split(",")
        }
    }


# JSON 登出接口
@App.route('/logout_json', methods=['POST'])
def logout_json():
    logout_user()
    return {"code": 0, "success": True, "message": "已退出登录"}


# CORS 支持（前后端分离开发模式备用，生产环境由 nginx 同源处理）
@App.after_request
def add_cors_headers(r):
    origin = request.headers.get('Origin')
    if origin:
        r.headers['Access-Control-Allow-Origin'] = origin
        r.headers['Access-Control-Allow-Credentials'] = 'true'
        r.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        r.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
    return r


# 事件响应
@App.route('/do', methods=['POST'])
@action_login_check
def do():
    try:
        cmd = request.form.get("cmd")
        data = request.form.get("data")
    except Exception as e:
        ExceptionUtils.exception_traceback(e)
        return {"code": -1, "msg": str(e)}
    if data:
        data = json.loads(data)
    return WebAction().action(cmd, data)


# 目录事件响应
@App.route('/dirlist', methods=['POST'])
@login_required
def dirlist():
    r = ['<ul class="jqueryFileTree" style="display: none;">']
    try:
        r = ['<ul class="jqueryFileTree" style="display: none;">']
        in_dir = request.form.get('dir')
        ft = request.form.get("filter")
        if not in_dir or in_dir == "/":
            if SystemUtils.get_system() == OsType.WINDOWS:
                partitions = SystemUtils.get_windows_drives()
                if partitions:
                    dirs = partitions
                else:
                    dirs = [os.path.join("C:/", f) for f in os.listdir("C:/")]
            else:
                dirs = [os.path.join("/", f) for f in os.listdir("/")]
        else:
            d = os.path.normpath(urllib.parse.unquote(in_dir))
            if not os.path.isdir(d):
                d = os.path.dirname(d)
            dirs = [os.path.join(d, f) for f in os.listdir(d)]
        dirs.sort()
        for ff in dirs:
            f = os.path.basename(ff)
            if not f:
                f = ff
            if os.path.isdir(ff):
                r.append('<li class="directory collapsed"><a rel="%s/">%s</a></li>' % (
                    ff.replace("\\", "/"), f.replace("\\", "/")))
            else:
                if ft != "HIDE_FILES_FILTER":
                    e = os.path.splitext(f)[1][1:]
                    r.append('<li class="file ext_%s"><a rel="%s">%s</a></li>' % (
                        e, ff.replace("\\", "/"), f.replace("\\", "/")))
        r.append('</ul>')
    except Exception as e:
        ExceptionUtils.exception_traceback(e)
        r.append('加载路径失败: %s' % str(e))
    r.append('</ul>')
    return make_response(''.join(r), 200)


# 禁止搜索引擎
@App.route('/robots.txt', methods=['GET', 'POST'])
def robots():
    return send_from_directory("", "robots.txt")


# 响应企业微信消息
@App.route('/wechat', methods=['GET', 'POST'])
def wechat():
    # 当前在用的交互渠道
    interactive_client = Message().get_interactive_client(SearchType.WX)
    if not interactive_client:
        return make_response("NAStool没有启用微信交互", 200)
    conf = interactive_client.get("config")
    sToken = conf.get('token')
    sEncodingAESKey = conf.get('encodingAESKey')
    sCorpID = conf.get('corpid')
    if not sToken or not sEncodingAESKey or not sCorpID:
        return
    wxcpt = WXBizMsgCrypt(sToken, sEncodingAESKey, sCorpID)
    sVerifyMsgSig = request.args.get("msg_signature")
    sVerifyTimeStamp = request.args.get("timestamp")
    sVerifyNonce = request.args.get("nonce")

    if request.method == 'GET':
        if not sVerifyMsgSig and not sVerifyTimeStamp and not sVerifyNonce:
            return "NAStool微信交互服务正常！<br>微信回调配置步聚：<br>1、在微信企业应用接收消息设置页面生成Token和EncodingAESKey并填入设置->消息通知->微信对应项，打开微信交互开关。<br>2、保存并重启本工具，保存并重启本工具，保存并重启本工具。<br>3、在微信企业应用接收消息设置页面输入此地址：http(s)://IP:PORT/wechat（IP、PORT替换为本工具的外网访问地址及端口，需要有公网IP并做好端口转发，最好有域名）。"
        sVerifyEchoStr = request.args.get("echostr")
        log.debug("收到微信验证请求: echostr= %s" % sVerifyEchoStr)
        ret, sEchoStr = wxcpt.VerifyURL(sVerifyMsgSig, sVerifyTimeStamp, sVerifyNonce, sVerifyEchoStr)
        if ret != 0:
            log.error("微信请求验证失败 VerifyURL ret: %s" % str(ret))
        # 验证URL成功，将sEchoStr返回给企业号
        return sEchoStr
    else:
        try:
            sReqData = request.data
            log.debug("收到微信消息：%s" % str(sReqData))
            ret, sMsg = wxcpt.DecryptMsg(sReqData, sVerifyMsgSig, sVerifyTimeStamp, sVerifyNonce)
            if ret != 0:
                log.error("解密微信消息失败 DecryptMsg ret = %s" % str(ret))
                return make_response("ok", 200)
            # 解析XML报文
            """
            1、消息格式：
            <xml>
               <ToUserName><![CDATA[toUser]]></ToUserName>
               <FromUserName><![CDATA[fromUser]]></FromUserName> 
               <CreateTime>1348831860</CreateTime>
               <MsgType><![CDATA[text]]></MsgType>
               <Content><![CDATA[this is a test]]></Content>
               <MsgId>1234567890123456</MsgId>
               <AgentID>1</AgentID>
            </xml>
            2、事件格式：
            <xml>
                <ToUserName><![CDATA[toUser]]></ToUserName>
                <FromUserName><![CDATA[UserID]]></FromUserName>
                <CreateTime>1348831860</CreateTime>
                <MsgType><![CDATA[event]]></MsgType>
                <Event><![CDATA[subscribe]]></Event>
                <AgentID>1</AgentID>
            </xml>            
            """
            dom_tree = xml.dom.minidom.parseString(sMsg.decode('UTF-8'))
            root_node = dom_tree.documentElement
            # 消息类型
            msg_type = DomUtils.tag_value(root_node, "MsgType")
            # 用户ID
            user_id = DomUtils.tag_value(root_node, "FromUserName")
            # 没的消息类型和用户ID的消息不要
            if not msg_type or not user_id:
                log.info("收到微信心跳报文...")
                return make_response("ok", 200)
            # 解析消息内容
            content = ""
            if msg_type == "event":
                # 事件消息
                event_key = DomUtils.tag_value(root_node, "EventKey")
                if event_key:
                    log.info("点击菜单：%s" % event_key)
                    keys = event_key.split('#')
                    if len(keys) > 2:
                        content = ModuleConf.WECHAT_MENU.get(keys[2])
            elif msg_type == "text":
                # 文本消息
                content = DomUtils.tag_value(root_node, "Content", default="")
            if content:
                # 处理消息内容
                WebAction().handle_message_job(msg=content,
                                               in_from=SearchType.WX,
                                               user_id=user_id,
                                               user_name=user_id)
            return make_response(content, 200)
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            log.error("微信消息处理发生错误：%s - %s" % (str(err), traceback.format_exc()))
            return make_response("ok", 200)


# Plex Webhook
@App.route('/plex', methods=['POST'])
def plex_webhook():
    if not SecurityHelper().check_mediaserver_ip(request.remote_addr):
        log.warn(f"非法IP地址的媒体服务器消息通知：{request.remote_addr}")
        return '不允许的IP地址请求'
    request_json = json.loads(request.form.get('payload', {}))
    log.debug("收到Plex Webhook报文：%s" % str(request_json))
    ThreadHelper().start_thread(WebhookEvent().plex_action, (request_json,))
    ThreadHelper().start_thread(SpeedLimiter().plex_action, (request_json,))
    return 'Ok'


# Jellyfin Webhook
@App.route('/jellyfin', methods=['POST'])
def jellyfin_webhook():
    if not SecurityHelper().check_mediaserver_ip(request.remote_addr):
        log.warn(f"非法IP地址的媒体服务器消息通知：{request.remote_addr}")
        return '不允许的IP地址请求'
    request_json = request.get_json()
    log.debug("收到Jellyfin Webhook报文：%s" % str(request_json))
    ThreadHelper().start_thread(WebhookEvent().jellyfin_action, (request_json,))
    ThreadHelper().start_thread(SpeedLimiter().jellyfin_action, (request_json,))
    return 'Ok'


@App.route('/emby', methods=['POST'])
# Emby Webhook
def emby_webhook():
    if not SecurityHelper().check_mediaserver_ip(request.remote_addr):
        log.warn(f"非法IP地址的媒体服务器消息通知：{request.remote_addr}")
        return '不允许的IP地址请求'
    request_json = json.loads(request.form.get('data', {}))
    log.debug("收到Emby Webhook报文：%s" % str(request_json))
    ThreadHelper().start_thread(WebhookEvent().emby_action, (request_json,))
    ThreadHelper().start_thread(SpeedLimiter().emby_action, (request_json,))
    return 'Ok'


# Telegram消息响应
@App.route('/telegram', methods=['POST', 'GET'])
def telegram():
    """
    {
        'update_id': ,
        'message': {
            'message_id': ,
            'from': {
                'id': ,
                'is_bot': False,
                'first_name': '',
                'username': '',
                'language_code': 'zh-hans'
            },
            'chat': {
                'id': ,
                'first_name': '',
                'username': '',
                'type': 'private'
            },
            'date': ,
            'text': ''
        }
    }
    """
    # 当前在用的交互渠道
    interactive_client = Message().get_interactive_client(SearchType.TG)
    if not interactive_client:
        return 'NAStool未启用Telegram交互'
    msg_json = request.get_json()
    if not SecurityHelper().check_telegram_ip(request.remote_addr):
        log.error("收到来自 %s 的非法Telegram消息：%s" % (request.remote_addr, msg_json))
        return '不允许的IP地址请求'
    if msg_json:
        message = msg_json.get("message", {})
        text = message.get("text")
        user_id = message.get("from", {}).get("id")
        log.info("收到Telegram消息：from=%s, text=%s" % (user_id, text))
        # 获取用户名
        user_name = message.get("from", {}).get("username")
        if text:
            # 检查权限
            if text.startswith("/"):
                if str(user_id) not in interactive_client.get("client").get_admin():
                    Message().send_channel_msg(channel=SearchType.TG,
                                               title="只有管理员才有权限执行此命令",
                                               user_id=user_id)
                    return '只有管理员才有权限执行此命令'
            else:
                if not str(user_id) in interactive_client.get("client").get_users():
                    message.send_channel_msg(channel=SearchType.TG,
                                             title="你不在用户白名单中，无法使用此机器人",
                                             user_id=user_id)
                    return '你不在用户白名单中，无法使用此机器人'
            WebAction().handle_message_job(msg=text,
                                           in_from=SearchType.TG,
                                           user_id=user_id,
                                           user_name=user_name)
    return 'Ok'


# Synology Chat消息响应
@App.route('/synology', methods=['POST', 'GET'])
def synology():
    """
    token: bot token
    user_id
    username
    post_id
    timestamp
    text
    """
    # 当前在用的交互渠道
    interactive_client = Message().get_interactive_client(SearchType.SYNOLOGY)
    if not interactive_client:
        return 'NAStool未启用Synology Chat交互'
    msg_data = request.form
    if not SecurityHelper().check_synology_ip(request.remote_addr):
        log.error("收到来自 %s 的非法Synology Chat消息：%s" % (request.remote_addr, msg_data))
        return '不允许的IP地址请求'
    if msg_data:
        token = msg_data.get("token")
        if not interactive_client.get("client").check_token(token):
            log.error("收到来自 %s 的非法Synology Chat消息：token校验不通过！" % request.remote_addr)
            return 'token校验不通过'
        text = msg_data.get("text")
        user_id = int(msg_data.get("user_id"))
        log.info("收到Synology Chat消息：from=%s, text=%s" % (user_id, text))
        # 获取用户名
        user_name = msg_data.get("username")
        if text:
            WebAction().handle_message_job(msg=text,
                                           in_from=SearchType.SYNOLOGY,
                                           user_id=user_id,
                                           user_name=user_name)
    return 'Ok'


# Slack消息响应
@App.route('/slack', methods=['POST'])
def slack():
    """
    # 消息
    {
        'client_msg_id': '',
        'type': 'message',
        'text': 'hello',
        'user': '',
        'ts': '1670143568.444289',
        'blocks': [{
            'type': 'rich_text',
            'block_id': 'i2j+',
            'elements': [{
                'type': 'rich_text_section',
                'elements': [{
                    'type': 'text',
                    'text': 'hello'
                }]
            }]
        }],
        'team': '',
        'client': '',
        'event_ts': '1670143568.444289',
        'channel_type': 'im'
    }
    # 快捷方式
    {
      "type": "shortcut",
      "token": "XXXXXXXXXXXXX",
      "action_ts": "1581106241.371594",
      "team": {
        "id": "TXXXXXXXX",
        "domain": "shortcuts-test"
      },
      "user": {
        "id": "UXXXXXXXXX",
        "username": "aman",
        "team_id": "TXXXXXXXX"
      },
      "callback_id": "shortcut_create_task",
      "trigger_id": "944799105734.773906753841.38b5894552bdd4a780554ee59d1f3638"
    }
    # 按钮点击
    {
      "type": "block_actions",
      "team": {
        "id": "T9TK3CUKW",
        "domain": "example"
      },
      "user": {
        "id": "UA8RXUSPL",
        "username": "jtorrance",
        "team_id": "T9TK3CUKW"
      },
      "api_app_id": "AABA1ABCD",
      "token": "9s8d9as89d8as9d8as989",
      "container": {
        "type": "message_attachment",
        "message_ts": "1548261231.000200",
        "attachment_id": 1,
        "channel_id": "CBR2V3XEX",
        "is_ephemeral": false,
        "is_app_unfurl": false
      },
      "trigger_id": "12321423423.333649436676.d8c1bb837935619ccad0f624c448ffb3",
      "client": {
        "id": "CBR2V3XEX",
        "name": "review-updates"
      },
      "message": {
        "bot_id": "BAH5CA16Z",
        "type": "message",
        "text": "This content can't be displayed.",
        "user": "UAJ2RU415",
        "ts": "1548261231.000200",
        ...
      },
      "response_url": "https://hooks.slack.com/actions/AABA1ABCD/1232321423432/D09sSasdasdAS9091209",
      "actions": [
        {
          "action_id": "WaXA",
          "block_id": "=qXel",
          "text": {
            "type": "plain_text",
            "text": "View",
            "emoji": true
          },
          "value": "click_me_123",
          "type": "button",
          "action_ts": "1548426417.840180"
        }
      ]
    }
    """
    # 只有本地转发请求能访问
    if not SecurityHelper().check_slack_ip(request.remote_addr):
        log.warn(f"非法IP地址的Slack消息通知：{request.remote_addr}")
        return '不允许的IP地址请求'

    # 当前在用的交互渠道
    interactive_client = Message().get_interactive_client(SearchType.SLACK)
    if not interactive_client:
        return 'NAStool未启用Slack交互'
    msg_json = request.get_json()
    if msg_json:
        if msg_json.get("type") == "message":
            channel = msg_json.get("client")
            text = msg_json.get("text")
            username = ""
        elif msg_json.get("type") == "block_actions":
            channel = msg_json.get("client", {}).get("id")
            text = msg_json.get("actions")[0].get("value")
            username = msg_json.get("user", {}).get("name")
        elif msg_json.get("type") == "event_callback":
            channel = msg_json.get("event", {}).get("client")
            text = re.sub(r"<@[0-9A-Z]+>", "", msg_json.get("event", {}).get("text"), flags=re.IGNORECASE).strip()
            username = ""
        elif msg_json.get("type") == "shortcut":
            channel = ""
            text = msg_json.get("callback_id")
            username = msg_json.get("user", {}).get("username")
        else:
            return "Error"
        WebAction().handle_message_job(msg=text,
                                       in_from=SearchType.SLACK,
                                       user_id=channel,
                                       user_name=username)
    return "Ok"


# Jellyseerr Overseerr订阅接口
@App.route('/subscribe', methods=['POST', 'GET'])
@require_auth
def subscribe():
    """
    {
        "notification_type": "{{notification_type}}",
        "event": "{{event}}",
        "subject": "{{subject}}",
        "message": "{{message}}",
        "image": "{{image}}",
        "{{media}}": {
            "media_type": "{{media_type}}",
            "tmdbId": "{{media_tmdbid}}",
            "tvdbId": "{{media_tvdbid}}",
            "status": "{{media_status}}",
            "status4k": "{{media_status4k}}"
        },
        "{{request}}": {
            "request_id": "{{request_id}}",
            "requestedBy_email": "{{requestedBy_email}}",
            "requestedBy_username": "{{requestedBy_username}}",
            "requestedBy_avatar": "{{requestedBy_avatar}}"
        },
        "{{issue}}": {
            "issue_id": "{{issue_id}}",
            "issue_type": "{{issue_type}}",
            "issue_status": "{{issue_status}}",
            "reportedBy_email": "{{reportedBy_email}}",
            "reportedBy_username": "{{reportedBy_username}}",
            "reportedBy_avatar": "{{reportedBy_avatar}}"
        },
        "{{comment}}": {
            "comment_message": "{{comment_message}}",
            "commentedBy_email": "{{commentedBy_email}}",
            "commentedBy_username": "{{commentedBy_username}}",
            "commentedBy_avatar": "{{commentedBy_avatar}}"
        },
        "{{extra}}": []
    }
    """
    req_json = request.get_json()
    if not req_json:
        return make_response("非法请求！", 400)
    notification_type = req_json.get("notification_type")
    if notification_type not in ["MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"]:
        return make_response("ok", 200)
    subject = req_json.get("subject")
    media_type = MediaType.MOVIE if req_json.get("media", {}).get("media_type") == "movie" else MediaType.TV
    tmdbId = req_json.get("media", {}).get("tmdbId")
    if not media_type or not tmdbId or not subject:
        return make_response("请求参数不正确！", 500)
    # 添加订阅
    code = 0
    msg = "ok"
    meta_info = MetaInfo(title=subject, mtype=media_type)
    if media_type == MediaType.MOVIE:
        code, msg, meta_info = Subscribe().add_rss_subscribe(mtype=media_type,
                                                             name=meta_info.get_name(),
                                                             year=meta_info.year,
                                                             mediaid=tmdbId)
        meta_info.user_name = req_json.get("request", {}).get("requestedBy_username")
        Message().send_rss_success_message(in_from=SearchType.API,
                                           media_info=meta_info)
    else:
        seasons = []
        for extra in req_json.get("extra", []):
            if extra.get("name") == "Requested Seasons":
                seasons = [int(str(sea).strip()) for sea in extra.get("value").split(", ") if str(sea).isdigit()]
                break
        for season in seasons:
            code, msg, meta_info = Subscribe().add_rss_subscribe(mtype=media_type,
                                                                 name=meta_info.get_name(),
                                                                 year=meta_info.year,
                                                                 mediaid=tmdbId,
                                                                 season=season)
            Message().send_rss_success_message(in_from=SearchType.API,
                                               media_info=meta_info)
    if code == 0:
        return make_response("ok", 200)
    else:
        return make_response(msg, 500)


# 备份配置文件
@App.route('/backup', methods=['POST'])
@login_required
def backup():
    """
    备份用户设置文件
    :return: 备份文件.zip_file
    """
    try:
        # 创建备份文件夹
        config_path = Path(Config().get_config_path())
        backup_file = f"bk_{time.strftime('%Y%m%d%H%M%S')}"
        backup_path = config_path / "backup_file" / backup_file
        backup_path.mkdir(parents=True)
        # 把现有的相关文件进行copy备份
        shutil.copy(f'{config_path}/config.yaml', backup_path)
        shutil.copy(f'{config_path}/default-category.yaml', backup_path)
        shutil.copy(f'{config_path}/user.db', backup_path)
        conn = sqlite3.connect(f'{backup_path}/user.db')
        cursor = conn.cursor()
        # 执行操作删除不需要备份的表
        table_list = [
            'SEARCH_RESULT_INFO',
            'RSS_TORRENTS',
            'DOUBAN_MEDIAS',
            'TRANSFER_HISTORY',
            'TRANSFER_UNKNOWN',
            'TRANSFER_BLACKLIST',
            'SYNC_HISTORY',
            'DOWNLOAD_HISTORY',
            'alembic_version'
        ]
        for table in table_list:
            cursor.execute(f"""DROP TABLE IF EXISTS {table};""")
        conn.commit()
        cursor.close()
        conn.close()
        zip_file = str(backup_path) + '.zip'
        if os.path.exists(zip_file):
            zip_file = str(backup_path) + '.zip'
        shutil.make_archive(str(backup_path), 'zip', str(backup_path))
        shutil.rmtree(str(backup_path))
    except Exception as e:
        ExceptionUtils.exception_traceback(e)
        return make_response("创建备份失败", 400)
    return send_file(zip_file)


# 上传文件到服务器
@App.route('/upload', methods=['POST'])
@login_required
def upload():
    try:
        files = request.files['file']
        temp_path = Config().get_temp_path()
        if not os.path.exists(temp_path):
            os.makedirs(temp_path)
        file_path = Path(temp_path) / files.filename
        files.save(str(file_path))
        return {"code": 0, "filepath": str(file_path)}
    except Exception as e:
        ExceptionUtils.exception_traceback(e)
        return {"code": 1, "msg": str(e), "filepath": ""}


# base64模板过滤器
@App.template_filter('b64encode')
def b64encode(s):
    return base64.b64encode(s.encode()).decode()


# split模板过滤器
@App.template_filter('split')
def split(string, char, pos):
    return string.split(char)[pos]


@App.route('/img')
@login_required
def Img():
    """
    图片中换服务
    """
    url = request.args.get('url')
    if not url:
        return make_response("参数错误", 400)
    # 计算Etag
    etag = hashlib.sha256(url.encode('utf-8')).hexdigest()
    # 检查协商缓存
    if_none_match = request.headers.get('If-None-Match')
    if if_none_match and if_none_match == etag:
        return make_response('', 304)
    # 获取图片数据
    response = Response(
        WebUtils.request_cache(url),
        mimetype='image/jpeg'
    )
    response.headers.set('Cache-Control', 'max-age=604800')
    response.headers.set('Etag', etag)
    return response
