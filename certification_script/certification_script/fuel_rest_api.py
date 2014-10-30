import os
import re
import json
import time
import urllib2
from functools import partial, wraps
from certification_script.cert_script import *

logger = None


def set_logger(log):
    global logger
    logger = log


class Urllib2HTTP(object):
    """
    class for making HTTP requests
    """

    allowed_methods = ('get', 'put', 'post', 'delete', 'patch', 'head')

    def __init__(self, root_url, headers=None, echo=False):
        """
        """
        if root_url.endswith('/'):
            self.root_url = root_url[:-1]
        else:
            self.root_url = root_url

        self.headers = headers if headers is not None else {}
        self.echo = echo

    def do(self, method, path, params=None):
        if path.startswith('/'):
            url = self.root_url + path
        else:
            url = self.root_url + '/' + path

        if method == 'get':
            assert params == {} or params is None
            data_json = None
        else:
            data_json = json.dumps(params)

        if self.echo:
            logger.info("HTTP: {} {}".format(method.upper(), url))

        request = urllib2.Request(url,
                                  data=data_json,
                                  headers=self.headers)
        if data_json is not None:
            request.add_header('Content-Type', 'application/json')

        request.get_method = lambda: method.upper()
        response = urllib2.urlopen(request)

        if response.code < 200 or response.code > 209:
            raise IndexError(url)

        content = response.read()

        if '' == content:
            return None

        return json.loads(content)

    def __getattr__(self, name):
        if name in self.allowed_methods:
            return partial(self.do, name)
        raise AttributeError(name)


def get_inline_param_list(url):
    format_param_rr = re.compile(r"\{([a-zA-Z_]+)\}")
    for match in format_param_rr.finditer(url):
        yield match.group(1)


class RestObj(object):
    name = None
    id = None

    def __init__(self, conn, **kwargs):
        self.__dict__.update(kwargs)
        self.__connection__ = conn

    def __str__(self):
        res = ["{}({}):".format(self.__class__.__name__, self.name)]
        for k, v in sorted(self.__dict__.items()):
            if k.startswith('__') or k.endswith('__'):
                continue
            if k != 'name':
                res.append("    {}={!r}".format(k, v))
        return "\n".join(res)

    def __getitem__(self, item):
        return getattr(self, item)


def make_call(method, url):
    def closure(obj, entire_obj=None, **data):
        if entire_obj is not None:
            if data != {}:
                raise ValueError("Both entire_obj and data provided")
            request_data = entire_obj
            result_url = url
        else:
            inline_params_vals = {}
            request_data = data.copy()
            for name in get_inline_param_list(url):
                if name in data:
                    inline_params_vals[name] = data[name]
                    del data[name]
                else:
                    inline_params_vals[name] = getattr(obj, name)
            result_url = url.format(**inline_params_vals)

        return obj.__connection__.do(method, result_url, params=request_data)
    return closure


PUT = partial(make_call, 'put')
GET = partial(make_call, 'get')
DELETE = partial(make_call, 'delete')


def with_timeout(tout, message):
    def closure(func):
        @wraps(func)
        def closure2(*dt, **mp):
            ctime = time.time()
            etime = ctime + tout

            while ctime < etime:
                if func(*dt, **mp):
                    return
                sleep_time = ctime + 1 - time.time()
                if sleep_time > 0:
                    time.sleep(sleep_time)
                ctime = time.time()
            raise RuntimeError("Timeout during " + message)
        return closure2
    return closure


# -------------------------------  ORM ----------------------------------------


def get_fuel_info(url):
    conn = Urllib2HTTP(url)
    return FuelInfo(conn)


class FuelInfo(RestObj):

    get_nodes = GET('api/nodes')
    get_clusters = GET('api/clusters')
    get_cluster = GET('api/clusters/{id}')

    @property
    def nodes(self):
        return NodeList([Node(self.__connection__, **node) for node
                         in self.get_nodes()])

    @property
    def free_nodes(self):
        return NodeList([Node(self.__connection__, **node) for node in
                         self.get_nodes() if not node['cluster']])

    @property
    def clusters(self):
        return [Cluster(self.__connection__, **cluster) for cluster
                in self.get_clusters()]


class Node(RestObj):

    get_info = GET('/api/nodes/{id}')
    network_roles = GET('/api/nodes/{id}/interfaces')
    network_roles_update = PUT('/api/nodes/{id}/interfaces')

    @property
    def networks(self):
        info = self.network_roles()
        result = {}

        for i in info:
            result[i['name']] = i

        return result

    @networks.setter
    def networks(self, mapping):
        info = self.network_roles()

        for i in range(len(info)):
            info[i] = mapping[info[i]['name']]

        url = '/api/nodes/{id}/interfaces'
        params = {'id': self.id}
        result_url = url.format(**params)

        self.__connection__.do('put', result_url, params=info)

    def set_node_name(self, name):
        self.__connection__.put('nodes', [{'id': self.id, 'name': name}])

    def get_network_data(self):
        node_info = self.get_info()
        return node_info.get('network_data')

    def get_roles(self):
        node_info = self.get_info()
        return node_info.get('roles'), node_info.get('pending_roles')

    def get_ip(self, network='public'):
        nets = self.get_network_data()
        for net in nets:
            if net['name'] == network:
                return net['ip']
        raise Exception('Network %s not found' % network)

class NodeList(list):
    allowed_roles = ['controller', 'compute', 'cinder', 'ceph-osd', 'mongo',
                     'zabbix-server']

    def __getattr__(self, name):
        if name in self.allowed_roles:
            return [node for node in self if name in node.roles]


class Cluster(RestObj):

    add_node_call = PUT('api/nodes')
    start_deploy = PUT('api/clusters/{id}/changes')
    get_status = GET('api/clusters/{id}')
    delete = DELETE('api/clusters/{id}')
    get_tasks_status = GET("api/tasks?tasks={id}")
    load_nodes = GET('api/nodes?cluster_id={id}')
    get_networks = GET('api/clusters/{id}/network_configuration/{net_provider}')
    configure_networks = PUT('api/clusters/{id}/network_configuration/{net_provider}')

    def __init__(self, *dt, **mp):
        super(Cluster, self).__init__(*dt, **mp)
        self.nodes = NodeList()
        self.network_roles = {}

    def check_exists(self):
        try:
            self.get_status()
            return True
        except urllib2.HTTPError as err:
            if err.code == 404:
                return False
            raise

    def add_node(self, node, roles, interfaces=None):
        data = {}
        data['pending_roles'] = roles
        data['cluster_id'] = self.id
        data['id'] = node.id
        data['pending_addition'] = True
        logger.debug("Adding node %s to cluster..." % node.id)
        self.add_node_call([data])
        self.nodes.append(node)

        if not interfaces is None:
            for iface in node.networks.keys():
                if node.networks[iface]['name'] not in self.network_roles:
                    for role in node.networks[iface]['assigned_networks']:
                        self.network_roles[role['name']] = role

            node_networks = node.networks

            for iface in node_networks.keys():
                node_networks[iface]['assigned_networks'] = []



            for iface in interfaces:
                for role in interfaces[iface]['networks']:
                    node_networks[iface]['assigned_networks'].append(self.network_roles[role])

            node.networks = node_networks

    def wait_operational(self, timeout):
        wo = lambda: self.get_status()['status'] == 'operational'
        with_timeout(timeout, "deploy cluster")(wo)()

    def deploy(self, timeout):
        logger.debug("Starting deploy...")
        self.start_deploy()

        self.wait_operational(timeout)

        def all_tasks_finished_ok(obj):
            ok = True
            for task in obj.get_tasks_status():
                if task['status'] == 'error':
                    raise Exception('Task execution error')
                elif task['status'] != 'ready':
                    ok = False
            return ok

        wto = with_timeout(timeout, "wait deployment finished")
        wto(all_tasks_finished_ok)(self)

    def dump_changes(self):
        dump_config(self.__connection__.root_url, self.id, self.name)

    def set_networks(self, net_description):
        configuration = self.get_networks()
        current_networks = configuration['networks']
        parameters = configuration['networking_parameters']
        for net in current_networks:
            net_desc = net_description['networks'].get(net['name'])
            if net_desc:
                net.update(net_desc)
        if net_description.get('networking_parameters'):
            parameters.update(net_description['networking_parameters'])
        self.configure_networks(**configuration)


def reflect_cluster(conn, cluster_id):
    c = Cluster(conn, id=cluster_id)
    c.nodes = NodeList([Node(conn, **data) for data in c.load_nodes()])
    return c


def get_all_nodes(conn):
    for node_desc in conn.get('api/nodes'):
        yield Node(conn, **node_desc)


def get_all_clusters(conn):
    for cluster_desc in conn.get('api/clusters'):
        yield Cluster(conn, **cluster_desc)


get_cluster_attributes = GET('api/clusters/{id}/attributes')


def get_cluster_id(name, conn):
    for cluster in get_all_clusters(conn):
        if cluster.name == name:
            logger.info('cluster name is %s' % name)
            logger.info('cluster id is %s' % cluster.id)
            return cluster.id


update_cluster_attributes = PUT('api/clusters/{id}/attributes')


sections = {
    'sahara': 'additional_components',
    'murano': 'additional_components',
    'ceilometer': 'additional_components',
    'volumes_ceph': 'storage',
    'images_ceph': 'storage',
    'ephemeral_ceph': 'storage',
    'objects_ceph': 'storage',
    'osd_pool_size': 'storage',
    'volumes_lvm': 'storage',
    'volumes_vmdk': 'storage',
    'tenant': 'access',
    'password': 'access',
    'user': 'access',
    'vc_password': 'vcenter',
    'cluster': 'vcenter',
    'host_ip': 'vcenter',
    'vc_user': 'vcenter',
    'use_vcenter': 'vcenter',
}


def create_empty_cluster(conn, cluster_desc, debug_mode=False):
    logger.info("Creating new cluster %s" % cluster_desc['name'])
    data = {}
    data['nodes'] = []
    data['tasks'] = []
    data['name'] = cluster_desc['name']
    data['release'] = cluster_desc['release']
    data['mode'] = cluster_desc.get('deployment_mode')
    data['net_provider'] = cluster_desc['settings'].get('net_provider')

    params = conn.post(path='/api/clusters', params=data)
    cluster = Cluster(conn, **params)

    settings = cluster_desc['settings']
    attributes = get_cluster_attributes(cluster)

    ed_attrs = attributes['editable']
    for option, value in settings.items():
        if option in sections:
            attr_val_dict = ed_attrs[sections[option]][option]
            attr_val_dict['value'] = value

    ed_attrs['common']['debug']['value'] = debug_mode
    update_cluster_attributes(cluster, attrs=attributes)

    return cluster
