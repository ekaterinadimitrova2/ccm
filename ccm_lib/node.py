# ccm node

import common, yaml, os, errno, signal, time, subprocess, shutil, sys

class Status():
    UNINITIALIZED = "UNINITIALIZED"
    UP = "UP"
    DOWN = "DOWN"
    DECOMMISIONNED = "DECOMMISIONNED"

class StartError(Exception):
    def __init__(self, msg, process):
        self.msg = msg
        self.process = process

    def __repr__(self):
        return self.msg

class Node():
    def __init__(self, name, cluster, auto_bootstrap, thrift_interface, storage_interface, jmx_port):
        self.name = name
        self.cluster = cluster
        self.status = Status.UNINITIALIZED
        self.auto_bootstrap = auto_bootstrap
        self.network_interfaces = { 'thrift' : thrift_interface, 'storage' : storage_interface }
        self.jmx_port = jmx_port
        self.pid = None

    def save(self):
        dir_name = self.get_path()
        if not os.path.exists(dir_name):
            os.mkdir(dir_name)
            for dir in self.get_directories():
                os.mkdir(os.path.join(dir_name, dir))

        filename = os.path.join(dir_name, 'node.conf')
        values = {
            'name' : self.name,
            'status' : self.status,
            'auto_bootstrap' : self.auto_bootstrap,
            'interfaces' : self.network_interfaces,
            'jmx_port' : self.jmx_port
        }
        if self.pid:
            values['pid'] = self.pid
        with open(filename, 'w') as f:
            yaml.dump(values, f)

    # TODO
    def stop(self):
        pass

    @staticmethod
    def load(path, name, cluster):
        node_path = os.path.join(path, name)
        filename = os.path.join(node_path, 'node.conf')
        with open(filename, 'r') as f:
            data = yaml.load(f)
        try:
            itf = data['interfaces'];
            node = Node(data['name'], cluster, data['auto_bootstrap'], itf['thrift'], itf['storage'], data['jmx_port'])
            node.status = data['status']
            if 'pid' in data:
                node.pid = int(data['pid'])
            return node
        except KeyError as k:
            raise common.LoadError("Error Loading " + filename + ", missing property: " + str(k))

    def get_directories(self):
        dirs = {}
        for i in ['data', 'commitlogs', 'saved_caches', 'logs', 'conf', 'bin']:
            dirs[i] = os.path.join(self.cluster.get_path(), self.name, i)
        return dirs

    def get_path(self):
        return os.path.join(self.cluster.get_path(), self.name)

    def get_conf_dir(self):
        return os.path.join(self.get_path(), 'conf')

    def update_configuration(self):
        self.update_yaml()
        self.update_log4j()
        self.update_envfile()

    def update_yaml(self):
        conf_file = os.path.join(self.get_conf_dir(), common.CASSANDRA_CONF)
        with open(conf_file, 'r') as f:
            data = yaml.load(f)

        data['auto_bootstrap'] = self.auto_bootstrap
        if 'seeds' in data:
            # cassandra 0.7
            data['seeds'] = self.cluster.get_seeds()
        else:
            # cassandra 0.8
            data['seed_provider'][0]['parameters'][0]['seeds'] = ','.join(self.cluster.get_seeds())
        data['listen_address'], data['storage_port'] = self.network_interfaces['storage']
        data['rpc_address'], data['rpc_port'] = self.network_interfaces['thrift']

        data['data_file_directories'] = [ os.path.join(self.get_path(), 'data') ]
        data['commitlog_directory'] = os.path.join(self.get_path(), 'commitlogs')
        data['saved_caches_directory'] = os.path.join(self.get_path(), 'saved_caches')

        if self.cluster.partitioner:
            data['partitioner'] = self.cluster.partitioner

        with open(conf_file, 'w') as f:
            yaml.dump(data, f, default_flow_style=False)

    def update_log4j(self):
        append_pattern='log4j.appender.R.File=';
        conf_file = os.path.join(self.get_conf_dir(), common.LOG4J_CONF)
        log_file = os.path.join(self.get_path(), 'logs', 'system.log')
        common.replace_in_file(conf_file, append_pattern, append_pattern + log_file)

    def update_envfile(self):
        jmx_port_pattern='JMX_PORT=';
        conf_file = os.path.join(self.get_conf_dir(), common.CASSANDRA_ENV)
        common.replace_in_file(conf_file, jmx_port_pattern, jmx_port_pattern + self.jmx_port)

    def get_status_string(self):
        if self.status == Status.UNINITIALIZED:
            return "%s (%s)" % (Status.DOWN, "Not initialized")
        else:
            return self.status

    def show(self, only_status=False, show_cluster=True):
        self.update_status()
        indent = ''.join([ " " for i in xrange(0, len(self.name) + 2) ])
        print "%s: %s" % (self.name, self.get_status_string())
        if not only_status:
          if show_cluster:
              print "%s%s=%s" % (indent, 'cluster', self.cluster.name)
          print "%s%s=%s" % (indent, 'auto_bootstrap', self.auto_bootstrap)
          print "%s%s=%s" % (indent, 'thrift', self.network_interfaces['thrift'])
          print "%s%s=%s" % (indent, 'storage', self.network_interfaces['storage'])
          print "%s%s=%s" % (indent, 'jmx_port', self.jmx_port)
          if self.pid:
              print "%s%s=%s" % (indent, 'pid', self.pid)

    def is_running(self):
        self.update_status()
        return self.status == Status.UP or self.status == Status.DECOMMISIONNED

    def is_live(self):
        self.update_status()
        return self.status == Status.UP

    def update_status(self):
        if self.pid is None:
            if self.status == Status.UP or self.status == Status.DECOMMISIONNED:
                self.status = Status.DOWN
            return

        old_status = self.status
        try:
            os.kill(self.pid, 0)
        except OSError, err:
            if err.errno == errno.ESRCH:
                # not running
                if self.status == Status.UP or self.status == Status.DECOMMISIONNED:
                    self.status = Status.DOWN
            elif err.errno == errno.EPERM:
                # no permission to signal this process
                if self.status == Status.UP or self.status == Status.DECOMMISIONNED:
                    self.status = Status.DOWN
            else:
                # some other error
                raise err
        else:
            if self.status == Status.DOWN or self.status == Status.UNINITIALIZED:
                self.status = Status.UP
        if not old_status == self.status:
            self.save()

    def start(self, cassandra_dir):
        cass_bin = os.path.join(cassandra_dir, 'bin', 'cassandra')
        env = common.make_cassandra_env(cassandra_dir, self.get_path())
        pidfile = os.path.join(self.get_path(), 'cassandra.pid')
        args = [ cass_bin, '-p', pidfile]
        p = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p

    def update_pid(self, process):
        pidfile = os.path.join(self.get_path(), 'cassandra.pid')
        try:
            with open(pidfile, 'r') as f:
                self.pid = int(f.readline().strip())
        except IOError:
            raise StartError('Problem starting node %s' % self.name, process)
        self.update_status()

    def stop(self):
        is_running = False
        if self.is_running():
            is_running = True
            os.kill(self.pid, signal.SIGKILL)
        self.pid = None
        self.save()
        return is_running

    def nodetool(self, cassandra_dir, cmd):
        nodetool = os.path.join(cassandra_dir, 'bin', 'nodetool')
        env = common.make_cassandra_env(cassandra_dir, self.get_path())
        host = self.network_interfaces['storage'][0]
        args = [ nodetool, '-h', host, '-p', str(self.jmx_port), cmd ]
        p = subprocess.Popen(args, env=env)
        p.wait()

    def run_cli(self, cassandra_dir):
        cli = os.path.join(cassandra_dir, 'bin', 'cassandra-cli')
        env = common.make_cassandra_env(cassandra_dir, self.get_path())
        host = self.network_interfaces['thrift'][0]
        port = self.network_interfaces['thrift'][1]
        args = [ 'cassandra-cli', '-h', host, '-p', str(port) , '--jmxport', str(self.jmx_port) ]
        sys.stdout.flush()
        os.execve(cli, args, env)

    def set_log_level(self, new_level):
        append_pattern='log4j.rootLogger=';
        conf_file = os.path.join(self.get_conf_dir(), common.LOG4J_CONF)
        l = new_level + ",stdout,R"
        common.replace_in_file(conf_file, append_pattern, append_pattern + l)

    def clear(self):
        data_dirs = [ 'data', 'commitlogs', 'saved_caches', 'logs']
        for d in data_dirs:
            full_dir = os.path.join(self.get_path(), d)
            shutil.rmtree(full_dir)
            os.mkdir(full_dir)

    def decommission(self):
        self.status = Status.DECOMMISIONNED
        self.save()
