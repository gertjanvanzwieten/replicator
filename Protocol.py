import Params, Response, Cache, time, socket, os, sys, re, calendar, logging


DNSCache = {}
DEBUG2 = 5 #logging level 5 is lower priority than than regular logging.DEBUG(==10)

def connect( addr ):

  assert Params.ONLINE, 'operating in off-line mode'
  if addr not in DNSCache:
    logging.debug(f'Requesting address info for {addr[0]}:{addr[1]}')
    DNSCache[ addr ] = socket.getaddrinfo( addr[ 0 ], addr[ 1 ], socket.AF_UNSPEC, socket.SOCK_STREAM )

  family, socktype, proto, canonname, sockaddr = DNSCache[ addr ][ 0 ]

  logging.info(f'Connecting to [{sockaddr[0]}]:[{sockaddr[1]}]')
  sock = socket.socket( family, socktype, proto )
  sock.setblocking( 0 )
  sock.connect_ex( sockaddr )

  return sock


class BlindProtocol:

  Response = None

  def __init__( self, request ):

    self.__socket = connect( request.addr )
    self.__sendbuf = request.recvbuf()

  def socket( self ):

    return self.__socket

  def recvbuf( self ):

    return b''

  def hasdata( self ):

    return True

  def send( self, sock ):

    bytes = sock.send( self.__sendbuf )
    self.__sendbuf = self.__sendbuf[ bytes: ]
    if not self.__sendbuf:
      self.Response = Response.BlindResponse

  def done( self ):

    pass


class HttpProtocol( Cache.File ):

  Response = None

  def __init__( self, request ):

    Cache.File.__init__( self, request.cache )

    if Params.STATIC and self.full():
      logging.info('Static mode; serving file directly from cache')
      self.__socket = None
      self.open_full()
      self.Response = Response.DataResponse
      return

    head = b'GET /%s HTTP/1.1' % request.path
    args = request.args.copy()
    args.pop( b'Accept-Encoding', None )
    args.pop( b'Range', None )
    stat = self.partial() or self.full()
    if stat:
      size = stat.st_size
      mtime = time.strftime( Params.TIMEFMT[0], time.gmtime( stat.st_mtime ) )
      if self.partial():
        logging.info(f'Requesting resume of partial file in cache: {size} bytes, {mtime}')
        args[ b'Range' ] = b'bytes=%i-' % size
      else:
        logging.info(f'Checking complete file in cache: {size} bytes, {mtime}')
        args[ b'If-Modified-Since' ] = mtime.encode()

    self.__socket = connect( request.addr )
    self.__sendbuf = b'\r\n'.join( [ head ] + list(map( b': '.join, iter(args.items()) )) + [ b'', b'' ] )
    self.__recvbuf = b''
    self.__parse = HttpProtocol.__parse_head

  def hasdata( self ):

    return bool( self.__sendbuf )

  def send( self, sock ):

    assert self.hasdata()

    bytes = sock.send( self.__sendbuf )
    self.__sendbuf = self.__sendbuf[ bytes: ]

  def __parse_head( self, chunk ):

    eol = chunk.find( b'\n' ) + 1
    if eol == 0:
      return 0

    line = chunk[ :eol ]
    logging.info(f'Server responds {line.rstrip().decode()}')
    fields = line.split()
    assert len( fields ) >= 3 and fields[ 0 ].startswith( b'HTTP/' ) and fields[ 1 ].isdigit(), 'invalid header line: %r' % line
    self.__status = int( fields[ 1 ] )
    self.__message = b' '.join( fields[ 2: ] )
    self.__args = {}
    self.__parse = HttpProtocol.__parse_args

    return eol

  def __parse_args( self, chunk ):

    eol = chunk.find( b'\n' ) + 1
    if eol == 0:
      return 0

    line = chunk[ :eol ]
    if b':' in line:
      logging.log(DEBUG2, f'> {line.rstrip().decode()}')
      key, value = line.split( b':', 1 )
      key = key.title()
      if key in self.__args:
        self.__args[ key ] += b'\r\n' + key + b': ' + value.strip()
      else:
        self.__args[ key ] = value.strip()
    elif line in ( b'\r\n', b'\n' ):
      self.__parse = None
    else:
      logging.info(f'Ignored header line: {line}')

    return eol

  def recv( self, sock ):

    assert not self.hasdata()

    chunk = sock.recv( Params.MAXCHUNK, socket.MSG_PEEK )
    assert chunk, 'server closed connection before sending a complete message header'
    self.__recvbuf += chunk
    while self.__parse:
      bytes = self.__parse( self, self.__recvbuf )
      if not bytes:
        sock.recv( len( chunk ) )
        return
      self.__recvbuf = self.__recvbuf[ bytes: ]
    sock.recv( len( chunk ) - len( self.__recvbuf ) )

    if self.__status == 200:

      self.open_new()
      if b'Last-Modified' in self.__args:
        for timefmt in Params.TIMEFMT:
          try:
            mtime = calendar.timegm( time.strptime( self.__args[b'Last-Modified'].decode(), timefmt ) )
          except ValueError:
            pass # try next time format string
          else: # time format string worked, so ignore remaining time format strings
            self.mtime = mtime
            break
        else: # all time format strings failed
          # raise exception presumably similar to above silenced exception
          raise ValueError("time data '%s' does not match formats %s" % (self.__args[ b'Last-Modified' ], b', '.join("'%s'" % timefmt for timefmt in Params.TIMEFMT)))
      if b'Content-Length' in self.__args:
        self.size = int( self.__args[ b'Content-Length' ] )
      if self.__args.pop( b'Transfer-Encoding', None ) == b'chunked':
        self.Response = Response.ChunkedDataResponse
      else:
        self.Response = Response.DataResponse

    elif self.__status == 206 and self.partial():

      range = self.__args.pop( b'Content-Range', b'none specified' )
      assert range.startswith( b'bytes ' ), 'invalid content-range: %s' % range
      range, size = range[ 6: ].split( b'/' )
      beg, end = range.split( b'-' )
      self.size = int( size )
      assert self.size == int( end ) + 1
      self.open_partial( int( beg ) )
      if self.__args.pop( b'Transfer-Encoding', None ) == b'chunked':
        self.Response = Response.ChunkedDataResponse
      else:
        self.Response = Response.DataResponse

    elif self.__status == 304 and self.full():

      self.open_full()
      self.Response = Response.DataResponse

    elif self.__status in ( 403, 416 ) and self.partial():

      self.remove_partial()
      self.Response = Response.BlindResponse

    else:

      self.Response = Response.BlindResponse

  def recvbuf( self ):

    return b'\r\n'.join( [ b'HTTP/1.1 %i %s' % ( self.__status, self.__message ) ] + list(map( b': '.join, iter(self.__args.items()) )) + [ b'', b'' ] )

  def args( self ):

    return self.__args.copy()

  def socket( self ):

    return self.__socket


class FtpProtocol( Cache.File ):

  Response = None

  def __init__( self, request ):

    Cache.File.__init__( self, request.cache )

    if Params.STATIC and self.full():
      self.__socket = None
      self.open_full()
      self.Response = Response.DataResponse
      return

    self.__socket = connect( request.addr )
    self.__path = request.path
    self.__sendbuf = b''
    self.__recvbuf = b''
    self.__handle = FtpProtocol.__handle_serviceready

  def socket( self ):

    return self.__socket

  def hasdata( self ):

    return self.__sendbuf != b''

  def send( self, sock ):

    assert self.hasdata()

    bytes = sock.send( self.__sendbuf )
    self.__sendbuf = self.__sendbuf[ bytes: ]

  def recv( self, sock ):

    assert not self.hasdata()

    chunk = sock.recv( Params.MAXCHUNK )
    assert chunk, 'server closed connection prematurely'
    self.__recvbuf += chunk
    while b'\n' in self.__recvbuf:
      reply, self.__recvbuf = self.__recvbuf.split( b'\n', 1 )
      logging.log(DEBUG2, f'S: {reply.rstrip().decode()}')
      if reply[:3].isdigit() and reply[3:4] != b'-':
        self.__handle( self, int( reply[ :3 ] ), reply[ 4: ] )
        if self.__sendbuf:
          logging.log(DEBUG2, f'C: {self.__sendbuf.rstrip().decode()}')

  def __handle_serviceready( self, code, line ):

    assert code == 220, 'server sends %i; expected 220 (service ready)' % code
    self.__sendbuf = b'USER anonymous\r\n'
    self.__handle = FtpProtocol.__handle_password

  def __handle_password( self, code, line ):

    assert code == 331, 'server sends %i; expected 331 (need password)' % code
    self.__sendbuf = b'PASS anonymous@\r\n'
    self.__handle = FtpProtocol.__handle_loggedin

  def __handle_loggedin( self, code, line ):

    assert code == 230, 'server sends %i; expected 230 (user logged in)' % code
    self.__sendbuf = b'TYPE I\r\n'
    self.__handle = FtpProtocol.__handle_binarymode

  def __handle_binarymode( self, code, line ):

    assert code == 200, 'server sends %i; expected 200 (binary mode ok)' % code
    if self.__socket.family == socket.AF_INET6:
        self.__sendbuf = b'EPSV\r\n'
        self.__handle = FtpProtocol.__handle_Epassivemode
    else:
        self.__sendbuf = b'PASV\r\n'
        self.__handle = FtpProtocol.__handle_passivemode

  def __handle_Epassivemode( self, code, line ):

    assert code == 229, 'server sends %i; expected 227 (e-passive mode)' % code
    match = re.search(r'\((.)\1\1(\d+)\1\)', line.decode())
    assert match, 'could not parse port from EPSV response (%s)' % line.decode()
    port = int(match.group(2))
    addr = (self.__socket.getpeername()[0], port)
    self.__socket = connect( addr )
    self.__sendbuf = b'SIZE %s\r\n' % self.__path
    self.__handle = FtpProtocol.__handle_size

  def __handle_passivemode( self, code, line ):

    assert code == 227, 'server sends %i; expected 227 (passive mode)' % code
    match = re.search(r'(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)', line.decode())
    assert match, 'could not parse address from PASV response (%s)' % line.decode()
    ip1, ip2, ip3, ip4, phi, plo = match.groups()
    addr = ('%s.%s.%s.%s'%(ip1,ip2,ip3,ip4), int(plo)+256*int(phi))
    self.__socket = connect( addr )
    self.__sendbuf = b'SIZE %s\r\n' % self.__path
    self.__handle = FtpProtocol.__handle_size

  def __handle_size( self, code, line ):

    if code == 550:
      self.Response = Response.NotFoundResponse
      return

    assert code == 213, 'server sends %i; expected 213 (file status)' % code
    self.size = int( line )
    logging.info(f'File size: {self.size}')
    self.__sendbuf = b'MDTM %s\r\n' % self.__path
    self.__handle = FtpProtocol.__handle_mtime

  def __handle_mtime( self, code, line ):

    if code == 550:
      self.Response = Response.NotFoundResponse
      return

    assert code == 213, 'server sends %i; expected 213 (file status)' % code
    self.mtime = calendar.timegm( time.strptime( line.decode().rstrip(), '%Y%m%d%H%M%S' ) )
    logging.info(f'Modification time: {time.strftime(Params.TIMEFMT[0], time.gmtime(self.mtime))}')
    stat = self.partial()
    if stat:
      self.__sendbuf = b'REST %i\r\n' % stat.st_size
      self.__handle = FtpProtocol.__handle_resume
    else:
      stat = self.full()
      if stat and stat.st_mtime == self.mtime:
        self.open_full()
        self.Response = Response.DataResponse
      else:
        self.open_new()
        self.__sendbuf = b'RETR %s\r\n' % self.__path
        self.__handle = FtpProtocol.__handle_data

  def __handle_resume( self, code, line ):

    assert code == 350, 'server sends %i; expected 350 (pending further information)' % code
    self.open_partial()
    self.__sendbuf = b'RETR %s\r\n' % self.__path
    self.__handle = FtpProtocol.__handle_data

  def __handle_data( self, code, line ):

    if code == 550:
      self.Response = Response.NotFoundResponse
      return

    assert code == 150, 'server sends %i; expected 150 (file ok)' % code
    self.Response = Response.DataResponse
