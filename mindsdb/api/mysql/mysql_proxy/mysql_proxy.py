"""
*******************************************************
 * Copyright (C) 2017 MindsDB Inc. <copyright@mindsdb.com>
 *
 * This file is part of MindsDB Server.
 *
 * MindsDB Server can not be copied and/or distributed without the express
 * permission of MindsDB Inc
 *******************************************************
"""

import os
import sys
import socketserver as SocketServer
import ssl
import traceback
import atexit
import tempfile
import socket
import struct
from functools import partial
import select
import base64
from typing import List, Dict

from lightwood.api import dtype

from mindsdb.utilities.wizards import make_ssl_cert
from mindsdb.utilities.config import Config
from mindsdb.api.mysql.mysql_proxy.data_types.mysql_packet import Packet
from mindsdb.api.mysql.mysql_proxy.controllers.session_controller import SessionController
from mindsdb.api.mysql.mysql_proxy.classes.client_capabilities import ClentCapabilities
from mindsdb.api.mysql.mysql_proxy.classes.server_capabilities import server_capabilities
from mindsdb.api.mysql.mysql_proxy.classes.sql_statement_parser import SqlStatementParser
from mindsdb.api.mysql.mysql_proxy.utilities import log

from mindsdb.api.mysql.mysql_proxy.external_libs.mysql_scramble import scramble as scramble_func

from mindsdb.api.mysql.mysql_proxy.libs.constants.mysql import (
    getConstName,
    CHARSET_NUMBERS,
    ERR,
    COMMANDS,
    TYPES,
    SERVER_VARIABLES,
    DEFAULT_AUTH_METHOD,
    SERVER_STATUS,
    FIELD_FLAG,
    CAPABILITIES
)

from mindsdb.api.mysql.mysql_proxy.data_types.mysql_packets import (
    ErrPacket,
    HandshakePacket,
    FastAuthFail,
    PasswordAnswer,
    HandshakeResponsePacket,
    OkPacket,
    SwitchOutPacket,
    SwitchOutResponse,
    CommandPacket,
    ColumnCountPacket,
    ColumnDefenitionPacket,
    ResultsetRowPacket,
    EofPacket,
    STMTPrepareHeaderPacket,
    BinaryResultsetRowPacket
)

from numpy import dtype as np_dtype
from pandas.api import types as pd_types

from mindsdb.interfaces.datastore.datastore import DataStore
from mindsdb.interfaces.model.model_interface import ModelInterface
from mindsdb.interfaces.database.integrations import IntegrationController
from mindsdb.interfaces.database.views import ViewController

import mindsdb.utilities.hooks as hooks

from mindsdb.api.mysql.mysql_proxy.executor.executor import Executor


def empty_fn():
    pass


def check_auth(username, password, scramble_func, salt, company_id, config):
    '''
    '''
    try:
        hardcoded_user = config['api']['mysql']['user']
        hardcoded_password = config['api']['mysql']['password']
        hardcoded_password_hash = scramble_func(hardcoded_password, salt)
        hardcoded_password = hardcoded_password.encode()

        if password is None:
            password = ''
        if isinstance(password, str):
            password = password.encode()

        integration = None
        integration_type = None
        extracted_username = username
        integrations_names = IntegrationController().get_all(company_id).keys()
        for integration_name in integrations_names:
            if username == f'{hardcoded_user}_{integration_name}':
                extracted_username = hardcoded_user
                integration = integration_name
                integration_type = IntegrationController().get(integration, company_id)['type']

        if extracted_username != hardcoded_user:
            log.warning(f'Check auth, user={username}: user mismatch')
            return {
                'success': False
            }

        if password != hardcoded_password and password != hardcoded_password_hash:
            log.warning(f'check auth, user={username}: password mismatch')
            return {
                'success': False
            }

        log.info(f'Check auth, user={username}: Ok')
        return {
            'success': True,
            'username': extracted_username,
            'integration': integration,
            'integration_type': integration_type
        }
    except Exception as e:
        log.error(f'Check auth, user={username}: ERROR')
        log.error(e)
        log.error(traceback.format_exc())


class RESPONSE_TYPE:
    __slots__ = ()
    OK = 'ok'
    TABLE = 'table'
    ERROR = 'error'


RESPONSE_TYPE = RESPONSE_TYPE()


class SQLAnswer:
    def __init__(self, resp_type: RESPONSE_TYPE, columns: List[Dict] = None, data: List[Dict] = None,
                 status: int = None, state_track: List[List] = None, error_code: int = None, error_message: str = None):
        self.resp_type = resp_type
        self.columns = columns
        self.data = data
        self.status = status
        self.state_track = state_track
        self.error_code = error_code
        self.error_message = error_message

    @property
    def type(self):
        return self.resp_type


class MysqlProxy(SocketServer.BaseRequestHandler):
    """
    The Main Server controller class
    """

    @staticmethod
    def server_close(srv):
        srv.server_close()

    def __init__(self, request, client_address, server):
        self.charset = 'utf8'
        self.charset_text_type = CHARSET_NUMBERS['utf8_general_ci']
        self.session = None
        self.client_capabilities = None
        super().__init__(request, client_address, server)

    def init_session(self, company_id=None):
        log.debug('New connection [{ip}:{port}]'.format(
            ip=self.client_address[0], port=self.client_address[1]))
        log.debug(self.__dict__)

        if self.server.connection_id >= 65025:
            self.server.connection_id = 0
        self.server.connection_id += 1
        self.connection_id = self.server.connection_id
        self.session = SessionController(
            server=self.server,
            company_id=company_id
        )

        if hasattr(self.server, 'salt') and isinstance(self.server.salt, str):
            self.salt = self.server.salt
        else:
            self.salt = base64.b64encode(os.urandom(15)).decode()

        self.socket = self.request
        self.logging = log

        self.current_transaction = None

        log.debug('session salt: {salt}'.format(salt=self.salt))

    def handshake(self):
        def switch_auth(method='mysql_native_password'):
            self.packet(SwitchOutPacket, seed=self.salt, method=method).send()
            switch_out_answer = self.packet(SwitchOutResponse)
            switch_out_answer.get()
            password = switch_out_answer.password
            if method == 'mysql_native_password' and len(password) == 0:
                password = scramble_func('', self.salt)
            return password

        def get_fast_auth_password():
            log.debug('Asking for fast auth password')
            self.packet(FastAuthFail).send()
            password_answer = self.packet(PasswordAnswer)
            password_answer.get()
            try:
                password = password_answer.password.value.decode()
            except Exception:
                log.warning('error: no password in Fast Auth answer')
                self.packet(ErrPacket, err_code=ERR.ER_PASSWORD_NO_MATCH, msg='Is not password in connection query.').send()
                return None
            return password

        username = None
        password = None

        log.debug('send HandshakePacket')
        self.packet(HandshakePacket).send()

        handshake_resp = self.packet(HandshakeResponsePacket)
        handshake_resp.get()
        if handshake_resp.length == 0:
            log.warning('HandshakeResponsePacket empty')
            self.packet(OkPacket).send()
            return False
        self.client_capabilities = ClentCapabilities(handshake_resp.capabilities.value)

        client_auth_plugin = handshake_resp.client_auth_plugin.value.decode()

        self.session.is_ssl = False

        if handshake_resp.type == 'SSLRequest':
            log.debug('switch to SSL')
            self.session.is_ssl = True

            ssl_context = ssl.SSLContext()
            ssl_context.load_cert_chain(self.server.cert_path)
            ssl_socket = ssl_context.wrap_socket(
                self.socket,
                server_side=True,
                do_handshake_on_connect=True
            )

            self.socket = ssl_socket
            handshake_resp = self.packet(HandshakeResponsePacket)
            handshake_resp.get()
            client_auth_plugin = handshake_resp.client_auth_plugin.value.decode()

        username = handshake_resp.username.value.decode()

        if client_auth_plugin != DEFAULT_AUTH_METHOD:
            if client_auth_plugin == 'mysql_native_password':
                password = switch_auth('mysql_native_password')
            else:
                new_method = 'caching_sha2_password' if client_auth_plugin == 'caching_sha2_password' else 'mysql_native_password'

                if new_method == 'caching_sha2_password' and self.session.is_ssl is False:
                    log.warning(f'Check auth, user={username}, ssl={self.session.is_ssl}, auth_method={client_auth_plugin}: '
                                'error: cant switch to caching_sha2_password without SSL')
                    self.packet(ErrPacket, err_code=ERR.ER_PASSWORD_NO_MATCH, msg='caching_sha2_password without SSL not supported').send()
                    return False

                log.debug(f'Check auth, user={username}, ssl={self.session.is_ssl}, auth_method={client_auth_plugin}: '
                          f'switch auth method to {new_method}')
                password = switch_auth(new_method)

                if new_method == 'caching_sha2_password':
                    if password == b'\x00':
                        password = ''
                    else:
                        password = get_fast_auth_password()
        elif 'caching_sha2_password' in client_auth_plugin:
            log.debug(
                f'Check auth, user={username}, ssl={self.session.is_ssl}, auth_method={client_auth_plugin}: '
                'check auth using caching_sha2_password'
            )
            password = handshake_resp.enc_password.value
            if password == b'\x00':
                password = ''
            else:
                # FIXME https://github.com/mindsdb/mindsdb/issues/1374
                # if self.session.is_ssl:
                #     password = get_fast_auth_password()
                # else:
                password = switch_auth()
        elif 'mysql_native_password' in client_auth_plugin:
            log.debug(f'Check auth, user={username}, ssl={self.session.is_ssl}, auth_method={client_auth_plugin}: '
                      'check auth using mysql_native_password')
            password = handshake_resp.enc_password.value
        else:
            log.debug(f'Check auth, user={username}, ssl={self.session.is_ssl}, auth_method={client_auth_plugin}: '
                      'unknown method, possible ERROR. Try to switch to mysql_native_password')
            password = switch_auth('mysql_native_password')

        try:
            self.session.database = handshake_resp.database.value.decode()
        except Exception:
            self.session.database = None
        log.debug(f'Check auth, user={username}, ssl={self.session.is_ssl}, auth_method={client_auth_plugin}: '
                  f'connecting to database {self.session.database}')

        auth_data = self.server.check_auth(username, password, scramble_func, self.salt, self.session.company_id)
        if auth_data['success']:
            self.session.username = auth_data['username']
            self.session.auth = True
            self.session.integration = auth_data['integration']
            self.session.integration_type = auth_data['integration_type']
            self.packet(OkPacket).send()
            return True
        else:
            self.packet(ErrPacket, err_code=ERR.ER_PASSWORD_NO_MATCH, msg=f'Access denied for user {username}').send()
            log.warning(f'Access denied for user {username}')
            return False

    def send_package_group(self, packages):
        string = b''.join([x.accum() for x in packages])
        self.socket.sendall(string)

    def answer_stmt_close(self, stmt_id):
        self.session.unregister_stmt(stmt_id)

    def send_query_answer(self, answer: SQLAnswer):
        if answer.type == RESPONSE_TYPE.TABLE:
            packages = []
            packages += self.get_tabel_packets(
                columns=answer.columns,
                data=answer.data
            )
            if answer.status is not None:
                packages.append(self.last_packet(status=answer.status))
            else:
                packages.append(self.last_packet())
            self.send_package_group(packages)
        elif answer.type == RESPONSE_TYPE.OK:
            self.packet(OkPacket, state_track=answer.state_track).send()
        elif answer.type == RESPONSE_TYPE.ERROR:
            self.packet(
                ErrPacket,
                err_code=answer.error_code,
                msg=answer.error_message
            ).send()

    def _get_column_defenition_packets(self, columns, data=None):
        if data is None:
            data = []
        packets = []
        for i, column in enumerate(columns):
            table_name = column.get('table_name', 'table_name')
            column_name = column.get('name', 'column_name')
            column_alias = column.get('alias', column_name)
            flags = column.get('flags', 0)
            if self.session.integration_type == 'mssql':
                # mssql raise error if value more then this.
                length = 0x2000
            else:
                if len(data) == 0:
                    length = 0xffff
                else:
                    length = 1
                    for row in data:
                        if isinstance(row, dict):
                            length = max(len(str(row[column_alias])), length)
                        else:
                            length = max(len(str(row[i])), length)

            packets.append(
                self.packet(
                    ColumnDefenitionPacket,
                    schema=column.get('database', 'mindsdb_schema'),
                    table_alias=column.get('table_alias', table_name),
                    table_name=table_name,
                    column_alias=column_alias,
                    column_name=column_name,
                    column_type=column['type'],
                    charset=column.get('charset', CHARSET_NUMBERS["utf8_unicode_ci"]),
                    max_length=length,
                    flags=flags
                )
            )
        return packets

    def get_tabel_packets(self, columns, data, status=0):
        # TODO remove columns order
        packets = [self.packet(ColumnCountPacket, count=len(columns))]
        packets.extend(self._get_column_defenition_packets(columns, data))

        if self.client_capabilities.DEPRECATE_EOF is False:
            packets.append(self.packet(EofPacket, status=status))

        packets += [self.packet(ResultsetRowPacket, data=x) for x in data]
        return packets

    def decode_utf(self, text):
        try:
            return text.decode('utf-8')
        except Exception as e:
            log.error(f'SQL contains non utf-8 values: {text}')
            self.packet(
                ErrPacket,
                err_code=ERR.ER_SYNTAX_ERROR,
                msg=str(e)
            ).send()
            raise

    def is_cloud_connection(self):
        ''' Determine source of connection. Must be call before handshake.
            Idea based on: real mysql connection does not send anything before server handshake, so
            soket should be in 'out' state. In opposite, clout connection sends '0000' right after
            connection. '0000' selected because in real mysql connection it should be lenght of package,
            and it can not be 0.
        '''
        if sys.platform != 'linux':
            return {
                'is_cloud': False
            }

        read_poller = select.poll()
        read_poller.register(self.request, select.POLLIN)
        events = read_poller.poll(0)

        if len(events) == 0:
            return {
                'is_cloud': False
            }

        first_byte = self.request.recv(4, socket.MSG_PEEK)
        if first_byte == b'\x00\x00\x00\x00':
            self.request.recv(4)
            client_capabilities = self.request.recv(8)
            client_capabilities = struct.unpack('L', client_capabilities)[0]

            company_id = self.request.recv(4)
            company_id = struct.unpack('I', company_id)[0]

            database_name_len = self.request.recv(2)
            database_name_len = struct.unpack('H', database_name_len)[0]

            database_name = ''
            if database_name_len > 0:
                database_name = self.request.recv(database_name_len).decode()

            return {
                'is_cloud': True,
                'client_capabilities': client_capabilities,
                'company_id': company_id,
                'database': database_name
            }

        return {
            'is_cloud': False
        }

    # --------------

    def to_mysql_columns(self, columns_list):

        result = []

        database = None if self.session.database == '' else self.session.database.lower()
        for column_record in columns_list:

            field_type = column_record.type

            column_type = TYPES.MYSQL_TYPE_VAR_STRING
            # is already in mysql protocol type?
            if isinstance(field_type, int):
                column_type = field_type
            # pandas checks
            elif isinstance(field_type, np_dtype):
                if pd_types.is_integer_dtype(field_type):
                    column_type = TYPES.MYSQL_TYPE_LONG
                elif pd_types.is_numeric_dtype(field_type):
                    column_type = TYPES.MYSQL_TYPE_DOUBLE
                elif pd_types.is_datetime64_any_dtype(field_type):
                    column_type = TYPES.MYSQL_TYPE_DATETIME
            # lightwood checks
            elif field_type == dtype.date:
                column_type = TYPES.MYSQL_TYPE_DATE
            elif field_type == dtype.datetime:
                column_type = TYPES.MYSQL_TYPE_DATETIME
            elif field_type == dtype.float:
                column_type = TYPES.MYSQL_TYPE_DOUBLE
            elif field_type == dtype.integer:
                column_type = TYPES.MYSQL_TYPE_LONG

            result.append({
                'database': column_record.database or database,
                #  TODO add 'original_table'
                'table_name': column_record.table_name,
                'name': column_record.name,
                'alias': column_record.alias or column_record.name,
                # NOTE all work with text-type, but if/when wanted change types to real,
                # it will need to check all types casts in BinaryResultsetRowPacket
                'type': column_type
            })
        return result

    def process_query(self, sql):
        executor = Executor(
            session=self.session,
            sqlserver=self
        )

        executor.query_execute(sql)

        if executor.error is not None:
            resp = SQLAnswer(
                resp_type=RESPONSE_TYPE.ERROR,
                error_code=executor.error['code'],
                error_message=executor.error['message']
            )
        elif executor.data is None:
            resp = SQLAnswer(
                resp_type=RESPONSE_TYPE.OK,
                state_track=executor.state_track,
            )
        else:

            resp = SQLAnswer(
                resp_type=RESPONSE_TYPE.TABLE,
                state_track=executor.state_track,
                columns=self.to_mysql_columns(executor.columns),
                data=executor.data,
                status=executor.server_status
            )
        return resp

    def answer_stmt_prepare(self, sql):
        executor = Executor(
            session=self.session,
            sqlserver=self
        )
        stmt_id = self.session.register_stmt(executor)

        executor.stmt_prepare(sql)

        packages = [
            self.packet(
                STMTPrepareHeaderPacket,
                stmt_id=stmt_id,
                num_columns=len(executor.columns),
                num_params=len(executor.params)
            )
        ]

        if len(executor.params) > 0:
            parameters_def = self.to_mysql_columns(executor.params)
            packages.extend(
                self._get_column_defenition_packets(parameters_def)
            )
            if self.client_capabilities.DEPRECATE_EOF is False:
                status = sum([SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT])
                packages.append(self.packet(EofPacket, status=status))

        if len(executor.columns) > 0:
            columns_def = self.to_mysql_columns(executor.columns)
            packages.extend(
                self._get_column_defenition_packets(columns_def)
            )

            if self.client_capabilities.DEPRECATE_EOF is False:
                status = sum([SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT])
                packages.append(self.packet(EofPacket, status=status))

        self.send_package_group(packages)

    def answer_stmt_execute(self, stmt_id, parameters):
        prepared_stmt = self.session.prepared_stmts[stmt_id]
        executor = prepared_stmt['statement']

        executor.stmt_execute(parameters)

        if executor.error is not None:
            resp = SQLAnswer(
                resp_type=RESPONSE_TYPE.ERROR,
                error_code=executor.error['code'],
                error_message=executor.error['message']
            )
            return self.send_query_answer(resp)

        elif executor.data is None:
            resp = SQLAnswer(
                resp_type=RESPONSE_TYPE.OK,
                state_track=executor.state_track
            )
            return self.send_query_answer(resp)

        # TODO prepared_stmt['type'] == 'lock' is not used but it works
        columns_def = self.to_mysql_columns(executor.columns)
        packages = [self.packet(ColumnCountPacket, count=len(columns_def))]

        packages.extend(self._get_column_defenition_packets(columns_def))

        if self.client_capabilities.DEPRECATE_EOF is False:
            packages.append(self.packet(EofPacket, status=0x0062))
        else:
            # send all
            for row in executor.data:
                packages.append(
                    self.packet(BinaryResultsetRowPacket, data=row, columns=columns_def)
                )

            server_status = executor.server_status or 0x0002
            packages.append(self.last_packet(status=server_status))
            prepared_stmt['fetched'] += len(executor.data)

        return self.send_package_group(packages)

    def answer_stmt_fetch(self, stmt_id, limit):
        prepared_stmt = self.session.prepared_stmts[stmt_id]
        executor = prepared_stmt['statement']
        fetched = prepared_stmt['fetched']

        if executor.error is not None:
            resp = SQLAnswer(
                resp_type=RESPONSE_TYPE.ERROR,
                error_code=executor.error['code'],
                error_message=executor.error['message']
            )
            return self.send_query_answer(resp)
        elif executor.data is None:
            resp = SQLAnswer(
                resp_type=RESPONSE_TYPE.OK,
                state_track=executor.state_track
            )
            return self.send_query_answer(resp)

        packages = []
        columns = self.to_mysql_columns(executor.columns)
        for row in executor.data[fetched:limit]:
            packages.append(
                self.packet(BinaryResultsetRowPacket, data=row, columns=columns)
            )

        prepared_stmt['fetched'] += len(executor.data[fetched:limit])

        if len(executor.data) <= limit + fetched:
            status = sum([
                SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT,
                SERVER_STATUS.SERVER_STATUS_LAST_ROW_SENT,
            ])
        else:
            status = sum([
                SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT,
                SERVER_STATUS.SERVER_STATUS_CURSOR_EXISTS,
            ])

        packages.append(self.last_packet(status=status))
        self.send_package_group(packages)

    def handle(self):
        """
        Handle new incoming connections
        :return:
        """
        self.server.hook_before_handle()

        log.debug('handle new incoming connection')
        cloud_connection = self.is_cloud_connection()
        self.init_session(company_id=cloud_connection.get('company_id'))
        if cloud_connection['is_cloud'] is False:
            if self.handshake() is False:
                return
        else:
            self.client_capabilities = ClentCapabilities(cloud_connection['client_capabilities'])
            self.session.database = cloud_connection['database']
            self.session.username = 'cloud'
            self.session.auth = True
            self.session.integration = None
            self.session.integration_type = None

        while True:
            log.debug('Got a new packet')
            p = self.packet(CommandPacket)

            try:
                success = p.get()
            except Exception:
                log.error('Session closed, on packet read error')
                log.error(traceback.format_exc())
                return

            if success is False:
                log.debug('Session closed by client')
                return

            log.debug('Command TYPE: {type}'.format(
                type=getConstName(COMMANDS, p.type.value)))

            command_names = {
                COMMANDS.COM_QUERY: 'COM_QUERY',
                COMMANDS.COM_STMT_PREPARE: 'COM_STMT_PREPARE',
                COMMANDS.COM_STMT_EXECUTE: 'COM_STMT_EXECUTE',
                COMMANDS.COM_STMT_FETCH: 'COM_STMT_FETCH',
                COMMANDS.COM_STMT_CLOSE: 'COM_STMT_CLOSE',
                COMMANDS.COM_QUIT: 'COM_QUIT',
                COMMANDS.COM_INIT_DB: 'COM_INIT_DB',
                COMMANDS.COM_FIELD_LIST: 'COM_FIELD_LIST'
            }

            command_name = command_names.get(p.type.value, f'UNKNOWN {p.type.value}')
            sql = None
            response = None
            error_type = None
            error_code = None
            error_text = None
            error_traceback = None

            try:
                if p.type.value == COMMANDS.COM_QUERY:
                    sql = self.decode_utf(p.sql.value)
                    sql = SqlStatementParser.clear_sql(sql)
                    log.debug(f'COM_QUERY: {sql}')
                    response = self.process_query(sql)
                elif p.type.value == COMMANDS.COM_STMT_PREPARE:
                    sql = self.decode_utf(p.sql.value)
                    self.answer_stmt_prepare(sql)
                elif p.type.value == COMMANDS.COM_STMT_EXECUTE:
                    self.answer_stmt_execute(p.stmt_id.value, p.parameters)
                elif p.type.value == COMMANDS.COM_STMT_FETCH:
                    self.answer_stmt_fetch(p.stmt_id.value, p.limit.value)
                elif p.type.value == COMMANDS.COM_STMT_CLOSE:
                    self.answer_stmt_close(p.stmt_id.value)
                elif p.type.value == COMMANDS.COM_QUIT:
                    log.debug('Session closed, on client disconnect')
                    self.session = None
                    break
                elif p.type.value == COMMANDS.COM_INIT_DB:
                    new_database = p.database.value.decode()

                    executor = Executor(
                        session=self.session,
                        sqlserver=self
                    )
                    executor.command_executor.change_default_db(new_database)

                    response = SQLAnswer(RESPONSE_TYPE.OK)
                elif p.type.value == COMMANDS.COM_FIELD_LIST:
                    # this command is deprecated, but console client still use it.
                    response = SQLAnswer(RESPONSE_TYPE.OK)
                else:
                    log.warning('Command has no specific handler, return OK msg')
                    log.debug(str(p))
                    # p.pprintPacket() TODO: Make a version of print packet
                    # that sends it to debug instead
                    response = SQLAnswer(RESPONSE_TYPE.OK)

            except Exception as e:
                error_type = 'unexpected'
                error_traceback = traceback.format_exc()
                log.error(
                    f'ERROR while executing query\n'
                    f'{error_traceback}\n'
                    f'{e}'
                )
                error_code = ERR.ER_SYNTAX_ERROR
                if hasattr(e, 'err_code'):
                    error_code = e.err_code
                response = SQLAnswer(
                    resp_type=RESPONSE_TYPE.ERROR,
                    error_code=error_code,
                    error_message=str(e)
                )

            if response is not None:
                self.send_query_answer(response)
                if response.type == RESPONSE_TYPE.ERROR:
                    error_text = response.error_message
                    error_code = response.error_code
                    error_type = error_type or 'expected'

            hooks.after_api_query(
                company_id=self.session.company_id,
                api='mysql',
                command=command_name,
                payload=sql,
                error_type=error_type,
                error_code=error_code,
                error_text=error_text,
                traceback=error_traceback
            )

    def packet(self, packetClass=Packet, **kwargs):
        """
        Factory method for packets

        :param packetClass:
        :param kwargs:
        :return:
        """
        p = packetClass(
            socket=self.socket,
            session=self.session,
            proxy=self,
            **kwargs
        )
        self.session.inc_packet_sequence_number()
        return p

    def last_packet(self, status=0x0002):
        if self.client_capabilities.DEPRECATE_EOF is True:
            return self.packet(OkPacket, eof=True, status=status)
        else:
            return self.packet(EofPacket, status=status)

    def set_context(self, context):
        if 'db' in context:
            self.session.database = context['db']

    def get_context(self, context):
        context = {}
        if self.session.database is not None:
            context['db'] = self.session.database

        return context

    @staticmethod
    def startProxy():
        """
        Create a server and wait for incoming connections until Ctrl-C
        """
        config = Config()

        cert_path = config['api']['mysql'].get('certificate_path')
        if cert_path is None or cert_path == '':
            cert_path = tempfile.mkstemp(prefix='mindsdb_cert_', text=True)[1]
            make_ssl_cert(cert_path)
            atexit.register(lambda: os.remove(cert_path))

        # TODO make it session local
        server_capabilities.set(
            CAPABILITIES.CLIENT_SSL,
            config['api']['mysql']['ssl']
        )

        host = config['api']['mysql']['host']
        port = int(config['api']['mysql']['port'])

        log.info(f'Starting MindsDB Mysql proxy server on tcp://{host}:{port}')

        SocketServer.TCPServer.allow_reuse_address = True
        server = SocketServer.ThreadingTCPServer((host, port), MysqlProxy)
        server.mindsdb_config = config
        server.check_auth = partial(check_auth, config=config)
        server.cert_path = cert_path
        server.connection_id = 0
        server.hook_before_handle = empty_fn

        server.original_model_interface = ModelInterface()
        server.original_data_store = DataStore()
        server.original_integration_controller = IntegrationController()
        server.original_view_controller = ViewController()

        atexit.register(MysqlProxy.server_close, srv=server)

        # Activate the server; this will keep running until you
        # interrupt the program with Ctrl-C
        log.info('Waiting for incoming connections...')
        server.serve_forever()


class Dummy:
    pass


class FakeMysqlProxy(MysqlProxy):
    def __init__(self, company_id):
        request = Dummy()
        client_address = ['', '']
        server = Dummy()
        server.connection_id = 0
        server.hook_before_handle = empty_fn
        server.original_model_interface = ModelInterface()
        server.original_data_store = DataStore()
        server.original_integration_controller = IntegrationController()
        server.original_view_controller = ViewController()

        self.charset = 'utf8'
        self.charset_text_type = CHARSET_NUMBERS['utf8_general_ci']
        self.client_capabilities = None

        self.request = request
        self.client_address = client_address
        self.server = server

        self.session = SessionController(
            server=self.server,
            company_id=company_id
        )
        self.session.database = 'mindsdb'

    def is_cloud_connection(self):
        return {
            'is_cloud': False
        }
