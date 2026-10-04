class IndexerConf(object):

    def __init__(self,
                 datas=None,
                 cookie=None,
                 name=None,
                 rule=None,
                 public=None,
                 proxy=False,
                 parser=None,
                 ua=None,
                 render=None,
                 language=None,
                 pri=None):
        if not datas:
            return
        # ID
        self.id = datas.get('id')
        # 名称
        self.name = datas.get('name') if not name else name
        # 域名
        self.domain = datas.get('domain')
        # 搜索
        self.search = datas.get('search', {})
        # 解析器
        self.parser = parser if parser is not None else datas.get('parser')
        # 是否启用渲染
        self.render = render if render is not None else datas.get("render")
        # 浏览
        self.browse = datas.get('browse', {})
        # 种子过滤
        self.torrents = datas.get('torrents', {})
        # 分类
        self.category = datas.get('category', {})
        # Cookie
        self.cookie = cookie
        # User-Agent
        self.ua = ua
        # 过滤规则
        self.rule = rule
        # 是否公开站点
        self.public = public
        # 是否使用代理
        self.proxy = proxy
        # 仅支持的特定语种
        self.language = language
        # 索引器优先级
        self.pri = pri if pri else 0
