#!/usr/bin/env python
# -*- coding: utf-8 -*-

from functools import wraps
import time
import json
import os
import logging
import pickle
from hashlib import sha1,md5
from urllib import urlencode
from requests_toolbelt import MultipartEncoder

import requests
import bencode
import captcha
'''
logging.basicConfig(level=logging.DEBUG,
                format='%(asctime)s %(filename)s[line:%(lineno)d] %(levelname)s %(message)s',
                datefmt='%a, %d %b %Y %H:%M:%S')
'''
BAIDUPCS_SERVER = 'pan.baidu.com'

#https://pcs.baidu.com/rest/2.0/pcs/manage?method=listhost -> baidu cdn
# uses CDN_DOMAIN/monitor.jpg to test speed for each CDN
api_template = 'http://%s/api/{0}' % BAIDUPCS_SERVER

class LoginFailed(Exception):
    """因为帐号原因引起的登录失败异常
    如果是超时则是返回Timeout的异常
    """
    pass

# experimental
class CancelledError(Exception):
    """
    用户取消文件上传
    """
    def __init__(self, msg):
        self.msg = msg
        Exception.__init__(self, msg)

    def __str__(self):
        return self.msg

    __repr__ = __str__


class BufferReader(MultipartEncoder):
    """将multipart-formdata转化为stream形式的Proxy类
    """
    def __init__(self, fields, boundary=None, callback=None, cb_args=(), cb_kwargs={}):
        self._callback = callback
        self._progress = 0
        self._cb_args = cb_args
        self._cb_kwargs = cb_kwargs
        super(BufferReader,self).__init__(fields,boundary)

    def read(self,size=None):
        chunk = super(BufferReader,self).read(size)
        self._progress += int(len(chunk))
        self._cb_kwargs.update({
            'size'    : self._len,
            'progress': self._progress
        })
        if self._callback:
            try:
                self._callback(*self._cb_args, **self._cb_kwargs)
            except: # catches exception from the callback
                raise CancelledError('The upload was cancelled.')
        return chunk

def check_login(func):
    """检查用户登录状态
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        ret = func(*args, **kwargs)
        if type(ret) == requests.Response:
            try:
                foo = json.loads(ret.content)
                if foo.has_key('errno') and foo['errno'] == -6:
                    logging.debug('Offline, deleting cookies file then relogin.')
                    path = '.{0}.cookies'.format(args[0].username)
                    if os.path.exists(path):
                        os.remove(path)
                    args[0]._initiate()
            except:
                pass
        return ret
    return wrapper


class BaseClass(object):
    """提供PCS类的基本方法
    """
    def __init__(self, username, password, api_template=api_template):
        self.session = requests.session()
        self.api_template = api_template
        self.username = username
        self.password = password
        self.user = {}
        self.progress_func = None
        if os.path.exists('.pcs-server'):
            import ConfigParser
            config = ConfigParser.ConfigParser()
            config.read('.pcs-server')
            self.set_pcs_server(config.get('server','fastest'))
        self._initiate()

    def test_fastest_mirror(self):
        """
        :returns: str -- 服务器地址
        """
        ret = requests.get('https://pcs.baidu.com/rest/2.0/pcs/manage?method=listhost').content
        serverlist = [server['host'] for server in json.loads(ret)['list']]
        url_pattern = 'http://{0}/monitor.jpg'
        time_record = []
        for server in serverlist:
            start = time.time()*1000
            requests.get(url_pattern.format(server))
            end = time.time()*1000
            time_record.append((end-start,server))
            logging.info('TEST %s %s ms' % (server,int(end-start)))
        return min(time_record)[1]


    def set_fastest_baidu_server(self):
        """自动测试并设置最快的百度服务器
        """
        global BAIDUPCS_SERVER
        BAIDUPCS_SERVER = self.test_fastest_mirror()
        with open('.pcs-server','w') as f:
            f.write('[server]\nfastest=%s' % BAIDUPCS_SERVER)
        logging.info('BAIDUPCS_SERVER='+BAIDUPCS_SERVER)

    def set_pcs_server(self,server):
        """手动设置百度网盘服务器
        :params server: 服务器地址或域名

        .. warning::
            不要加 http:// 和末尾的 /
        """
        global BAIDUPCS_SERVER
        BAIDUPCS_SERVER = server

    def _remove_empty_items(self, data):
        for k, v in data.copy().items():
            if v is None:
                data.pop(k)

    def _initiate(self):
        if not self._load_cookies():
            self.session.get('http://www.baidu.com')
            self.user['token'] = self._get_token()
            self._login()
        else:
            self.user['token'] = self._get_token()

    def _save_cookies(self):
        cookies_file = '.{0}.cookies'.format(self.username)
        with open(cookies_file,'w') as f:
            pickle.dump(requests.utils.dict_from_cookiejar(self.session.cookies), f)

    def _load_cookies(self):
        cookies_file = '.{0}.cookies'.format(self.username)
        logging.debug('cookies file:' + cookies_file)
        if os.path.exists(cookies_file):
            logging.debug('%s cookies file has already existed.' % self.username)
            with open(cookies_file) as cookies_file:
                cookies = requests.utils.cookiejar_from_dict(pickle.load(cookies_file))
                logging.debug(str(cookies))
                self.session.cookies = cookies
                self.user['BDUSS'] = self.session.cookies['BDUSS']
                return True
        else:
            return False
    def _get_token(self):
        #Token
        ret = self.session.get('https://passport.baidu.com/v2/api/?getapi&tpl=mn&apiver=v3&class=login&tt=%s&logintype=dialogLogin&callback=0' % int(time.time())).text.replace('\'','\"')
        foo = json.loads(ret)
        logging.info('token %s' % foo['data']['token'])
        return foo['data']['token']

    def _get_captcha(self):
        #Captcha
        ret = self.session.get('https://passport.baidu.com/v2/api/?logincheck&token=%s&tpl=mn&apiver=v3&tt=%s&username=%s&isphone=false&callback=0' % (self.user['token'], int(time.time()), self.username)).text.replace('\'','\"')
        foo = json.loads(ret)
        code_string = foo['data']['codeString']
        if code_string:
            logging.debug("requiring captcha")
            url = "https://passport.baidu.com/cgi-bin/genimage?" + code_string
            jpeg = self.session.get(url).content
            captcha.show(jpeg)
            verifycode = raw_input('captcha > ')
        else:
            verifycode = ""
        return (code_string,verifycode)

    def _login(self):
        #Login
        code_string, captcha = self._get_captcha()
        login_data = {'staticpage':'http://www.baidu.com/cache/user/html/v3Jump.html',
                        'charset':'UTF-8',
                        'token':self.user['token'],
                        'tpl':'mn',
                        'apiver':'v3',
                        'tt':str(int(time.time())),
                        'codestring':code_string,
                        'isPhone':'false',
                        'safeflg':'0',
                        'u':'http://www.baidu.com/',
                        'quick_user':'0',
                        'usernamelogin':'1',
                        'splogin':'rate',
                        'username':self.username,
                        'password':self.password,
                        'verifycode':captcha,
                        'mem_pass':'on',
                        'ppui_logintime':'5000',
                        'callback':'parent.bd__pcbs__oa36qm'}
        result = self.session.post('https://passport.baidu.com/v2/api/?login',data=login_data)
        if not result.ok:
            raise LoginFailed('Logging failed.')
        logging.info('COOKIES' + str(self.session.cookies))
        try:
            self.user['BDUSS'] = self.session.cookies['BDUSS']
        except:
            raise LoginFailed('Logging failed.')
        logging.info('user %s Logged in BDUSS: %s' % (self.username, self.user['BDUSS']))
        self._save_cookies()

    @check_login
    def _request(self, uri, method=None, url=None, extra_params=None,
                 data=None, files=None, callback=None, **kwargs):
        params = {
            'method': method,
            'app_id':"250528",
            'BDUSS':self.user['BDUSS'],
            't':int(time.time()),
            'bdstoken':self.user['token']
        }
        if extra_params:
            params.update(extra_params)
            self._remove_empty_items(params)
        if not url:
            url = self.api_template.format(uri)
        if data or files:
            if '?' in url:
                api = "%s&%s" % (url, urlencode(params))
            else:
                api = '%s?%s' % (url, urlencode(params))

            if data:
                self._remove_empty_items(data)
                response = self.session.post(api, data=data, verify=False,
                                         **kwargs)
            else:
                self._remove_empty_items(files)

                body = BufferReader(files, callback=callback)
                headers = {
                    "Content-Type": body.content_type
                }

                response = self.session.post(api, data=body, verify=False,headers=headers,**kwargs)
        else:
            api = url
            if uri == 'filemanager':
               response = self.session.post(api, params=params, verify=False, **kwargs)
            else:
                response = self.session.get(api, params=params, verify=False, **kwargs)
        return response


class PCS(BaseClass):
    def __init__(self,  username, password, api_template=api_template):
        """
        :param username: 百度网盘的用户名
        :type username: str

        :param password: 百度网盘的密码
        :type password: str
        """
        super(PCS, self).__init__(username, password, api_template)



    def quota(self, **kwargs):
        """获得配额信息
        :return requests.Response

            .. note::
                返回正确时返回的 Reponse 对象 content 中的数据结构

                {"errno":0,"total":配额字节数,"used":已使用字节数,"request_id":请求识别号}
        """
        return self._request('quota', **kwargs)


    def upload(self, dir, file_handler, filename, ondup="newcopy", callback=None, **kwargs):
        """上传单个文件（<2G）.

        | 百度PCS服务目前支持最大2G的单个文件上传。
        | 如需支持超大文件（>2G）的断点续传，请参考下面的“分片文件上传”方法。

        :param dir: 网盘中文件的保存路径（不包含文件名）。
                            必须以 / 开头。

                            .. warning::
                                * 注意本接口的 dir 参数不包含文件名，只包含路径
                                * 路径长度限制为1000；
                                * 径中不能包含以下字符：``\\\\ ? | " > < : *``；
                                * 文件名或路径名开头结尾不能是 ``.``
                                  或空白字符，空白字符包括：
                                  ``\\r, \\n, \\t, 空格, \\0, \\x0B`` 。
        :param file_handler: 上传文件对象 。(e.g. ``open('foobar', 'rb')`` )

                            .. warning::
                                注意不要使用 .read() 方法.
        :type file_handler: file
        :param callback: 上传进度回调函数
            需要包含 size 和 progress 名字的参数

        :param filename:

        :param ondup: （可选）

                      * 'overwrite'：表示覆盖同名文件；
                      * 'newcopy'：表示生成文件副本并进行重命名，命名规则为“
                        文件名_日期.后缀”。
        :return: requests.Response 对象

            .. note::
                返回正确时返回的 Reponse 对象 content 中的数据结构

                {"path":"服务器文件路径","size":文件大小,"ctime":创建时间,"mtime":修改时间,"md5":"文件md5值","fs_id":服务器文件识别号,"isdir":是否为目录,"request_id":请求识别号}

        """


        params = {
            'dir': dir,
            'ondup': ondup,
            'filename':filename
        }

        files = {'file': (filename,file_handler)}

        url = 'https://pcs.baidu.com/rest/2.0/pcs/file'
        return self._request('file', 'upload', url=url, extra_params=params,
                             files=files, callback=callback, **kwargs)

    def upload_tmpfile(self, file_handler, callback=None, **kwargs):
        """分片上传—文件分片及上传.

        百度 PCS 服务支持每次直接上传最大2G的单个文件。

        如需支持上传超大文件（>2G），则可以通过组合调用分片文件上传的
        ``upload_tmpfile`` 方法和 ``upload_superfile`` 方法实现：

        1. 首先，将超大文件分割为2G以内的单文件，并调用 ``upload_tmpfile``
           将分片文件依次上传；
        2. 其次，调用 ``upload_superfile`` ，完成分片文件的重组。

        除此之外，如果应用中需要支持断点续传的功能，
        也可以通过分片上传文件并调用 ``upload_superfile`` 接口的方式实现。

        :param file_handler: 上传文件对象 。(e.g. ``open('foobar', 'rb')`` )

                            .. warning::
                                注意不要使用 .read() 方法.
        :type file_handler: file

        :param callback: 上传进度回调函数
            需要包含 size 和 progress 名字的参数

        :param ondup: （可选）

                      * 'overwrite'：表示覆盖同名文件；
                      * 'newcopy'：表示生成文件副本并进行重命名，命名规则为“
                        文件名_日期.后缀”。
        :type ondup: str

        :return: requests.Response

            .. note::
                这个对象的内容中的 md5 字段为合并文件的凭依

            .. note::
                返回正确时返回的 Reponse 对象 content 中的数据结构

                {"md5":"片段的 md5 值","request_id":请求识别号}



        """

        params = {
            'type': 'tmpfile'
        }
        files = {'file': (str(int(time.time())),file_handler)}
        url = 'https://pcs.baidu.com/rest/2.0/pcs/file'
        return self._request('file', 'upload', url=url, extra_params=params,callback=callback,
                             files=files, **kwargs)

    def upload_superfile(self, remote_path, block_list, ondup="newcopy", **kwargs):
        """分片上传—合并分片文件.

        与分片文件上传的 ``upload_tmpfile`` 方法配合使用，
        可实现超大文件（>2G）上传，同时也可用于断点续传的场景。

        :param remote_path: 网盘中文件的保存路径（包含文件名）。
                            必须以  开头。

                            .. warning::
                                * 路径长度限制为1000；
                                * 径中不能包含以下字符：``\\\\ ? | " > < : *``；
                                * 文件名或路径名开头结尾不能是 ``.``
                                  或空白字符，空白字符包括：
                                  ``\\r, \\n, \\t, 空格, \\0, \\x0B`` 。
        :param block_list: 子文件内容的 MD5 值列表；子文件至少两个，最多1024个。
        :type block_list: list
        :param ondup: （可选）

                      * 'overwrite'：表示覆盖同名文件；
                      * 'newcopy'：表示生成文件副本并进行重命名，命名规则为“
                        文件名_日期.后缀”。
        :return: Response 对象

            .. note::
                返回正确时返回的 Reponse 对象 content 中的数据结构

                {"path":"服务器文件路径","size":文件大小,"ctime":创建时间,"mtime":修改时间,"md5":"文件md5值","fs_id":服务器文件识别号,"isdir":是否为目录,"request_id":请求识别号}

        """

        params = {
            'path': remote_path,
            'ondup': ondup
        }
        data = {
            'param': json.dumps({'block_list': block_list}),
        }
        url = 'https://pcs.baidu.com/rest/2.0/pcs/file'
        return self._request('file', 'createsuperfile', url=url, extra_params=params,
                             data=data, **kwargs)

    def download(self, remote_path, **kwargs):
        """下载单个文件。

        download 接口支持HTTP协议标准range定义，通过指定range的取值可以实现
        断点下载功能。 例如：如果在request消息中指定“Range: bytes=0-99”，
        那么响应消息中会返回该文件的前100个字节的内容；
        继续指定“Range: bytes=100-199”，
        那么响应消息中会返回该文件的第二个100字节内容::

          >>> headers = {'Range': 'bytes=0-99'}
          >>> pcs = PCS('username','password')
          >>> pcs.download('/test_sdk/test.txt', headers=headers)

        :param remote_path: 网盘中文件的路径（包含文件名）。
                            必须以 / 开头。

                            .. warning::
                                * 路径长度限制为1000；
                                * 径中不能包含以下字符：``\\\\ ? | " > < : *``；
                                * 文件名或路径名开头结尾不能是 ``.``
                                  或空白字符，空白字符包括：
                                  ``\\r, \\n, \\t, 空格, \\0, \\x0B`` 。
        :return: Response 对象
        """

        params = {
            'path': remote_path,
        }
        url = 'https://pcs.baidu.com/rest/2.0/pcs/file'
        return self._request('file', 'download', url=url,
                             extra_params=params, **kwargs)

    def mkdir(self, remote_path, **kwargs):
        """为当前用户创建一个目录.

        :param remote_path: 网盘中目录的路径，必须以 / 开头。

                            .. warning::
                                * 路径长度限制为1000；
                                * 径中不能包含以下字符：``\\\\ ? | " > < : *``；
                                * 文件名或路径名开头结尾不能是 ``.``
                                  或空白字符，空白字符包括：
                                  ``\\r, \\n, \\t, 空格, \\0, \\x0B`` 。
        :return: Response 对象

            .. note::
                返回正确时返回的 Reponse 对象 content 中的数据结构

                {"fs_id":服务器文件识别号,"path":"路径","ctime":创建时间,"mtime":修改时间,"status":0,"isdir":1,"errno":0,"name":"文件路径"}

        """

        data = {
            'path': remote_path,
            'isdir': "1",
            "size":"",
            "block_list": "[]"
        }
        # 奇怪的是创建新目录的method是post
        return self._request('create', 'post', data=data, **kwargs)

    def list_files(self, remote_path, by="name", order="desc",
                   limit=None, **kwargs):
        """获取目录下的文件列表.

        :param remote_path: 网盘中目录的路径，必须以 / 开头。

                            .. warning::
                                * 路径长度限制为1000；
                                * 径中不能包含以下字符：``\\\\ ? | " > < : *``；
                                * 文件名或路径名开头结尾不能是 ``.``
                                  或空白字符，空白字符包括：
                                  ``\\r, \\n, \\t, 空格, \\0, \\x0B`` 。
        :param by: 排序字段，缺省根据文件类型排序：

                   * time（修改时间）
                   * name（文件名）
                   * size（大小，注意目录无大小）
        :param order: “asc”或“desc”，缺省采用降序排序。

                      * asc（升序）
                      * desc（降序）
        :param limit: 返回条目控制，参数格式为：n1-n2。

                      返回结果集的[n1, n2)之间的条目，缺省返回所有条目；
                      n1从0开始。
        :return: requests.Response 对象

            .. note::
                返回正确时返回的 Reponse 对象 content 中的数据结构

                {
                    "errno":0,
                    "list":[
                        {"fs_id":服务器文件识别号"path":"路径","server_filename":"服务器文件名（不汗含路径）","size":文件大小,"server_mtime":服务器修改时间,"server_ctime":服务器创建时间,"local_mtime":本地修改时间,"local_ctime":本地创建时间,"isdir":是否是目录,"category":类型,"md5":"md5值"}……等等
                           ],
                    "request_id":请求识别号
                }

        """
        if order == "desc":
            desc = "1"
        else:
            desc = "0"

        params = {
            'dir': remote_path,
            'order': by,
            'desc': desc
        }
        return self._request('list', 'list', extra_params=params, **kwargs)


    def move(self, path_list, dest, **kwargs):
        """
        移动文件或文件夹

        :param path_list: 在百度盘上要移动的源文件path
        :type path_list: list

        :param dest: 要移动到的目录
        :type dest: str

        """
        def __path(path):
            if path.endswith('/'):
                return path.split('/')[-2]
            else:
                return os.path.basename(path)
        params = {
            'opera':'move'
        }
        data = {
            'filelist': json.dumps([{
                        "path":path,
                        "dest":dest,
                        "newname":__path(path)} for path in path_list]),
        }
        url = 'http://{0}/api/filemanager'.format(BAIDUPCS_SERVER)
        return self._request('filemanager', 'move', url=url, data=data, extra_params=params, **kwargs)

    def copy(self, path_list, dest, **kwargs):
        """
        复制文件或文件夹

        :param path_list: 在百度盘上要复制的源文件path
        :type path_list: list

        :param dest: 要复制到的目录
        :type dest: str

        """
        def __path(path):
            if path.endswith('/'):
                return path.split('/')[-2]
            else:
                return os.path.basename(path)
        params = {
            'opera':'copy'
        }
        data = {
            'filelist': json.dumps([{
                        "path":path,
                        "dest":dest,
                        "newname":__path(path)} for path in path_list]),
        }
        url = 'http://{0}/api/filemanager'.format(BAIDUPCS_SERVER)
        return self._request('filemanager', 'move', url=url, data=data, extra_params=params, **kwargs)

    def delete(self, path_list, **kwargs):
        """
        删除文件或文件夹

        :param path_list: 待删除的文件或文件夹列表,每一项为服务器路径
        :type path_list: list


        """
        data = {
                'filelist': json.dumps([path for path in path_list])
        }
        url = 'http://{0}/api/filemanager?opera=delete'.format(BAIDUPCS_SERVER)
        return self._request('filemanager', 'delete', url=url, data=data, **kwargs)

    def list_streams(self, file_type, start=0, limit=1000, order='time', desc='1',
                     filter_path=None, **kwargs):
        """以视频、音频、图片及文档四种类型的视图获取所创建应用程序下的
        文件列表.

        :param file_type: 类型分为video audio image doc other exe torrent
        :param start: 返回条目控制起始值，缺省值为0。
        :param limit: 返回条目控制长度，缺省为1000，可配置。
        :param filter_path: 需要过滤的前缀路径，如：/album

                            .. warning::
                                * 路径长度限制为1000；
                                * 径中不能包含以下字符：``\\\\ ? | " > < : *``；
                                * 文件名或路径名开头结尾不能是 ``.``
                                  或空白字符，空白字符包括：
                                  ``\\r, \\n, \\t, 空格, \\0, \\x0B`` 。
        :return: requests.Response 对象, 结构和 list_files 相同
        """
        if file_type == 'doc':
            file_type = '4'
        elif file_type  == 'video':
            file_type = '1'
        elif file_type == 'image':
            file_type = '3'
        elif file_type == 'torrent':
            file_type = '7'
        elif file_type == 'other':
            file_type = '6'
        elif file_type == 'audio':
            file_type = '2'
        elif file_type == 'exe':
            file_type = '5'

        params = {
            'category': file_type,
            'pri': '-1',
            'start': start,
            'num': limit,
            'order': order,
            'desc': desc,
            'filter_path': filter_path,
        }
        url = 'http://pan.baidu.com/api/categorylist'
        return self._request('categorylist', 'list', url=url, extra_params=params,
                             **kwargs)

    def add_download_task(self, source_url, remote_path, **kwargs):
        """
        添加离线任务

        :param source_url: 下载的地址,不可以是 magnet 协议

            .. note::
                需要支持 ``magnet`` 地址可以在本地使用 ``magnet`` 地址生成种子文件后调用 **add_local_bt_task**
        :type source_url: str

        """
        data = {
            'method':'add_task',
            'source_url': source_url,
            'save_path': remote_path,
        }
        url = 'http://{0}/rest/2.0/services/cloud_dl'.format(BAIDUPCS_SERVER)
        return self._request('services/cloud_dl', 'add_task', url=url,
                             data=data, **kwargs)

    def _calc_torrent_sha1(self, torrent_content):
        metainfo = bencode.bdecode(torrent_content)
        info = metainfo['info']
        return sha1(bencode.bencode(info)).hexdigest()

    def add_local_bt_task(self, torrent_path, save_path='/',selected_idx=0, **kwargs):
        """
        添加本地BT任务

        :param torrent_path: 本地种子的路径

        :param save_path: 远程保存路径

        :param selected_idx: 要下载的文件序号，0为所有，默认为0

        :return: requests.Response

            .. note::
                返回正确时返回的 Reponse 对象 content 中的数据结构

                {"task_id":任务编号,"rapid_download":是否已经完成（急速下载）,"request_id":请求识别号}

        """
        torrent_handler = open(torrent_path,'rb')

        basename = os.path.basename(torrent_path)
        with open(torrent_path,'rb') as foo:
            torrent_sha1 = self._calc_torrent_sha1(foo.read())

        if selected_idx != 0:
            selected_idx = ','.join(map(str,selected_idx))

        # 首先上传种子文件
        ret = self.upload('/', torrent_handler, basename).content
        remote_path = json.loads(ret)['path']
        logging.debug('REMOTE PATH:' + remote_path)

        #开始下载
        data = {
            'method':'add_task',
            'file_sha1':torrent_sha1,
            'save_path': save_path,
            'selected_idx': selected_idx,
            'task_from': '1',
            'source_path': remote_path,
            'type': '2' # 2 is torrent file
        }
        url = 'http://{0}/rest/2.0/services/cloud_dl'.format(BAIDUPCS_SERVER)
        return self._request('create', 'add_task', url=url, data=data, **kwargs)

    def get_remote_file_info(self, remote_path, type='2', **kwargs):
        """获得百度网盘里种子的信息

        :return: requests.Response
        """
        params = {
            'type': type,
            'source_path': remote_path
        }
        url = 'http://{0}/rest/2.0/services/cloud_dl'.format(BAIDUPCS_SERVER)
        return self._request('cloud_dl', 'query_sinfo', url=url, extra_params=params, **kwargs)

    def query_download_tasks(self, task_ids, operate_type=1, **kwargs):
        """根据任务ID号，查询离线下载任务信息及进度信息。

        :param task_ids: 要查询的任务 ID字符串 列表
        :type task_ids: list or tuple
        :param operate_type:
                            * 0：查任务信息
                            * 1：查进度信息，默认为1

        :return: requests.Response

            .. note::
                返回正确时返回的 Reponse 对象 content 中的数据结构

                给出一个范例

                {
                    "task_info":
                        {"70970481":{
                                "status":"0",

                                "file_size":"122328178",

                                "finished_size":"122328178",

                                "create_time":"1391620757",

                                "start_time":"1391620757",

                                "finish_time":"1391620757",

                                "save_path":"\/",

                                "source_url":"\/saki-nation04gbcn.torrent",

                                "task_name":"[KTXP][Saki-National][04][GB_CN][720p]",

                                "od_type":"2",

                                "file_list":[
                                    {
                                        "file_name":"[KTXP][Saki-National][04][GB_CN][720p].mp4",

                                        "file_size":"122328178"
                                    }
                                ],

                                "result":0

                                }
                        },

                        "request_id":861570268

                }


        """

        params = {
            'task_ids': ','.join(map(str, task_ids)),
            'op_type': operate_type,
        }
        url = 'http://{0}/rest/2.0/services/cloud_dl'.format(BAIDUPCS_SERVER)
        return self._request('services/cloud_dl', 'query_task', url=url,
                             extra_params=params, **kwargs)

    def download_tasks_number(self):
        """获取离线任务总数

        :return: int
        """
        ret = self.list_download_tasks().content
        foo = json.loads(ret)
        return foo['total']

    def list_download_tasks(self, need_task_info="1", asc="0", start=0,create_time=None, limit=1000, status="255",source_url=None,remote_path=None, **kwargs):
        """查询离线下载任务ID列表及任务信息.

        :param need_task_info: 是否需要返回任务信息:
                               * 0：不需要
                               * 1：需要，默认为1
        :param start: 查询任务起始位置，默认为0。
        :param limit: 设定返回任务数量，默认为10。
        :param asc:
                   * 0：降序，默认值
                   * 1：升序
        :param create_time: 任务创建时间，默认为空。
        :type create_time: int
        :param status: 任务状态，默认为空。

            .. note::
                任务状态有
                       0:下载成功

                       1:下载进行中

                       2:系统错误

                       3:资源不存在

                       4:下载超时

                       5:资源存在但下载失败

                       6:存储空间不足

                       7:目标地址数据已存在, 8:任务取消.
        :type status: int
        :param source_url: 源地址URL，默认为空。
        :param remote_path: 文件保存路径，默认为空。

                            .. warning::
                                * 路径长度限制为1000；
                                * 径中不能包含以下字符：``\\\\ ? | " > < : *``；
                                * 文件名或路径名开头结尾不能是 ``.``
                                  或空白字符，空白字符包括：
                                  ``\\r, \\n, \\t, 空格, \\0, \\x0B`` 。
        :param expires: 请求失效时间，如果有，则会校验。
        :type expires: int
        :return: Response 对象

             .. note::
                返回正确时返回的 Reponse 对象 content 中的数据结构

                    {
                        "task_info": [

                            {

                                "task_id": "任务识别号",

                                "od_type": "2",

                                "source_url": "原地址，bt任务为种子在服务器上的路径，否则为原始URL",

                                "save_path": "保存路径",

                                "rate_limit": "速度限制，0为不限",

                                "timeout": "0",

                                "callback": "",

                                "status": "任务状态",

                                "create_time": "创建时间",

                                "task_name": "任务名"

                            },……等等

                        ],

                        "total": 总数,

                        "request_id": 请求识别号

                    }
        """

        params = {
            'start': start,
            'limit': limit,
            'status': status,
            'need_task_info': need_task_info,
            'asc':asc,
            'source_url':source_url,
            'remote_path':remote_path,
            'create_time': create_time

        }
        url = 'http://{0}/rest/2.0/services/cloud_dl'.format(BAIDUPCS_SERVER)
        return self._request('services/cloud_dl', 'list_task', url=url, extra_params=params, **kwargs)

    def cancel_download_task(self, task_id, expires=None, **kwargs):
        """取消离线下载任务.

        :param task_id: 要取消的任务ID号。
        :type task_id: str
        :param expires: 请求失效时间，如果有，则会校验。
        :type expires: int
        :return: requests.Response
        """

        data = {
            'expires': expires,
            'task_id': task_id,
        }
        url = 'http://{0}/rest/2.0/services/cloud_dl'.format(BAIDUPCS_SERVER)
        return self._request('services/cloud_dl', 'cancle_task',
                             data=data, **kwargs)

    def list_recycle_bin(self, order="time", desc="1", start=0, limit=1000, page=1, **kwargs):
        #Done
        """获取回收站中的文件及目录列表.

        :param start: 返回条目的起始值，缺省值为0
        :param limit: 返回条目的长度，缺省值为1000
        :return: requests.Response

            格式同 list_files
        """

        params = {
            'start': start,
            'num': limit,
            'dir':'/',
            'order':order,
            'desc':desc
        }
        url = 'http://{0}/api/recycle/list'.format(BAIDUPCS_SERVER)
        return self._request('recycle', 'list', url=url, extra_params=params, **kwargs)

    def restore_recycle_bin(self, fs_ids, **kwargs):

        """批量还原文件或目录（非强一致接口，调用后请sleep1秒 ）.

        :param fs_ids: 所还原的文件或目录在 PCS 的临时唯一标识 ID 的列表。
        :type fs_ids: list or tuple
        :return: requests.Response 对象
        """

        data = {
            'filelist': json.dumps([fs_id for fs_id in fs_ids])
        }
        url = 'http://{0}/api/recycle/restore'.format(BAIDUPCS_SERVER)
        return self._request('recycle', 'restore', data=data, **kwargs)

    def clean_recycle_bin(self, **kwargs):

        """清空回收站.

        :return: requests.Response
        """

        url = 'http://{0}/api/recycle/clear'.format(BAIDUPCS_SERVER)
        return self._request('recycle', 'clear', url=url, **kwargs)
