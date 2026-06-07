import re
import urllib.parse
import html
import asyncio
import sqlite3
import logging
import socket
import struct
import uuid
from pathlib import Path
import xml.etree.ElementTree as ET
from aiohttp import web
from mutagen import File as MutagenFile

# Configuration
MEDIA_DIRS = ["/path/to/your/music"]
BIND_IP = "192.168.1.100" # Replace with your actual local IP
PORT = 8080
UUID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "python-audiophile-dlna"))
SERVER_NAME = "Python Audiophile Server"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DLNAServer")

# ==========================================
# 1. Database & Metadata Scanner
# ==========================================
class MediaLibrary:
    def __init__(self, db_path="media.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE,
                title TEXT,
                artist TEXT,
                album TEXT,
                mime_type TEXT,
                size INTEGER,
                duration REAL
            )
        ''')
        self.conn.commit()

    def scan_directories(self, directories):
        logger.info("Starting media scan...")
        cursor = self.conn.cursor()
        supported_exts = {'.flac', '.wav', '.mp3', '.dsf', '.m4a'}
        
        for directory in directories:
            path = Path(directory)
            for file_path in path.rglob("*"):
                if file_path.is_file() and file_path.suffix.lower() in supported_exts:
                    self._index_file(file_path, cursor)
        self.conn.commit()
        logger.info("Scan complete.")

    def _index_file(self, file_path, cursor):
        try:
            # Extract Metadata
            audio = MutagenFile(file_path, easy=True)
            if audio is None: return

            title = audio.get('title', [file_path.stem])[0]
            artist = audio.get('artist', ['Unknown Artist'])[0]
            album = audio.get('album', ['Unknown Album'])[0]
            mime_type = f"audio/{file_path.suffix.lower().strip('.')}"
            size = file_path.stat().st_size
            duration = audio.info.length if hasattr(audio, 'info') else 0

            cursor.execute('''
                INSERT OR REPLACE INTO media (path, title, artist, album, mime_type, size, duration)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (str(file_path), title, artist, album, mime_type, size, duration))
        except Exception as e:
            logger.error(f"Failed to parse {file_path}: {e}")

# ==========================================
# 2. SSDP Discovery (Multicast UDP)
# ==========================================
class SSDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, ip, port, uuid):
        self.ip = ip
        self.port = port
        self.uuid = uuid

    def connection_made(self, transport):
        self.transport = transport
        # Join multicast group
        sock = transport.get_extra_info('socket')
        mreq = struct.pack("4sl", socket.inet_aton("239.255.255.250"), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        logger.info("SSDP Multicast group joined.")

    def datagram_received(self, data, addr):
        msg = data.decode('utf-8', errors='ignore')
        if msg.startswith('M-SEARCH'):
            # Respond to DLNA renderers looking for servers
            response = (
                "HTTP/1.1 200 OK\r\n"
                "CACHE-CONTROL: max-age=1800\r\n"
                f"DATE: {socket.gethostname()}\r\n"
                "EXT:\r\n"
                f"LOCATION: http://{self.ip}:{self.port}/description.xml\r\n"
                f"SERVER: Linux/5.0 UPnP/1.0 {SERVER_NAME}/1.0\r\n"
                "ST: upnp:rootdevice\r\n"
                f"USN: uuid:{self.uuid}::upnp:rootdevice\r\n"
                "\r\n"
            )
            self.transport.sendto(response.encode('utf-8'), addr)

# ==========================================
# 3. HTTP Server & UPnP SOAP Endpoints
# ==========================================
class UPnPServer:
    def __init__(self, library, ip, port, uuid):
        self.library = library
        self.ip = ip
        self.port = port
        self.uuid = uuid
        self.app = web.Application(middlewares=[self.dlna_headers_middleware])
        self.setup_routes()

    @web.middleware
    async def dlna_headers_middleware(self, request, handler):
        # Critical for Pioneer N-50AE compatibility
        response = await handler(request)
        response.headers['transferMode.dlna.org'] = 'Streaming'
        response.headers['contentFeatures.dlna.org'] = 'DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000'
        return response

    def setup_routes(self):
        self.app.router.add_get('/description.xml', self.handle_description)
        self.app.router.add_post('/ContentDirectory/control', self.handle_soap)
        self.app.router.add_get('/media/{id}', self.handle_media)

    async def handle_description(self, request):
        # UPnP Device Architecture XML
        xml = f"""<?xml version="1.0"?>
        <root xmlns="urn:schemas-upnp-org:device-1-0">
            <specVersion><major>1</major><minor>0</minor></specVersion>
            <device>
                <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
                <friendlyName>{SERVER_NAME}</friendlyName>
                <manufacturer>Custom Python Server</manufacturer>
                <modelName>Audiophile DLNA</modelName>
                <UDN>uuid:{self.uuid}</UDN>
                <serviceList>
                    <service>
                        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
                        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
                        <controlURL>/ContentDirectory/control</controlURL>
                        <eventSubURL>/ContentDirectory/event</eventSubURL>
                        <SCPDURL>/ContentDirectory.xml</SCPDURL>
                    </service>
                </serviceList>
            </device>
        </root>"""
        return web.Response(text=xml, content_type='text/xml')

    async def handle_soap(self, request):
        body = await request.text()
        
        # Safely extract the ObjectID using regex to bypass complex SOAP namespaces
        match = re.search(r'<ObjectID[^>]*>(.*?)</ObjectID>', body)
        object_id = match.group(1) if match else "0"

        cursor = self.library.conn.cursor()
        items_xml = ""
        item_count = 0

        # ==========================================
        # ROOT VIEW: List all Artists
        # ==========================================
        if object_id == "0":
            cursor.execute("SELECT DISTINCT artist FROM media ORDER BY artist")
            artists = cursor.fetchall()
            
            for (artist,) in artists:
                if not artist: continue
                # Create a URL-safe virtual ID for the artist
                safe_artist = urllib.parse.quote(artist)
                v_id = f"artist_{safe_artist}"
                
                items_xml += f"""
                <container id="{v_id}" parentID="0" restricted="1" searchable="0">
                    <dc:title>{html.escape(artist)}</dc:title>
                    <upnp:class>object.container.person.musicArtist</upnp:class>
                </container>"""
                item_count += 1

        # ==========================================
        # ARTIST VIEW: List Albums by selected Artist
        # ==========================================
        elif object_id.startswith("artist_"):
            artist = urllib.parse.unquote(object_id.replace("artist_", ""))
            cursor.execute("SELECT DISTINCT album FROM media WHERE artist=? ORDER BY album", (artist,))
            albums = cursor.fetchall()

            for (album,) in albums:
                if not album: continue
                safe_album = urllib.parse.quote(album)
                v_id = f"album_{urllib.parse.quote(artist)}_{safe_album}"
                
                items_xml += f"""
                <container id="{v_id}" parentID="{object_id}" restricted="1" searchable="0">
                    <dc:title>{html.escape(album)}</dc:title>
                    <upnp:class>object.container.album.musicAlbum</upnp:class>
                </container>"""
                item_count += 1

        # ==========================================
        # ALBUM VIEW: List Tracks in selected Album
        # ==========================================
        elif object_id.startswith("album_"):
            # Extract artist and album from the virtual ID
            parts = object_id.split('_', 2)
            artist = urllib.parse.unquote(parts[1])
            album = urllib.parse.unquote(parts[2])
            
            # Assuming you might want to sort by track number in the future, 
            # currently sorting by title to keep the query simple.
            cursor.execute("SELECT id, title, mime_type, size, duration FROM media WHERE artist=? AND album=? ORDER BY title", (artist, album))
            tracks = cursor.fetchall()

            for track in tracks:
                t_id, title, mime_type, size, duration = track
                # Format duration for UPnP (H:MM:SS.F)
                m, s = divmod(int(duration), 60)
                h, m = divmod(m, 60)
                dur_str = f"{h}:{m:02d}:{s:02d}.000"

                items_xml += f"""
                <item id="{t_id}" parentID="{object_id}" restricted="1">
                    <dc:title>{html.escape(title)}</dc:title>
                    <upnp:class>object.item.audioItem.musicTrack</upnp:class>
                    <upnp:artist>{html.escape(artist)}</upnp:artist>
                    <upnp:album>{html.escape(album)}</upnp:album>
                    <res protocolInfo="http-get:*:{mime_type}:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000" 
                         size="{size}" duration="{dur_str}">http://{self.ip}:{self.port}/media/{t_id}</res>
                </item>"""
                item_count += 1

        # ==========================================
        # WRAP AND RETURN DIDL-LITE XML
        # ==========================================
        didl = f"""<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" 
                              xmlns:dc="http://purl.org/dc/elements/1.1/" 
                              xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
            {items_xml}
        </DIDL-Lite>"""

        soap_response = f"""<?xml version="1.0" encoding="utf-8"?>
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
            <s:Body>
                <u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
                    <Result>{html.escape(didl)}</Result>
                    <NumberReturned>{item_count}</NumberReturned>
                    <TotalMatches>{item_count}</TotalMatches>
                    <UpdateID>1</UpdateID>
                </u:BrowseResponse>
            </s:Body>
        </s:Envelope>"""
        
        return web.Response(text=soap_response, content_type='text/xml')

    async def handle_media(self, request):
        # Aiohttp's FileResponse automatically handles HTTP 206 Range Requests natively!
        # This is vital for the Pioneer N-50AE to seek through tracks.
        media_id = request.match_info['id']
        cursor = self.library.conn.cursor()
        cursor.execute("SELECT path FROM media WHERE id=?", (media_id,))
        row = cursor.fetchone()
        
        if row and Path(row[0]).exists():
            return web.FileResponse(row[0])
        return web.Response(status=404)

# ==========================================
# 4. Main Event Loop
# ==========================================
async def main():
    library = MediaLibrary()
    library.scan_directories(MEDIA_DIRS)

    loop = asyncio.get_running_loop()
    
    # Start SSDP
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: SSDPProtocol(BIND_IP, PORT, UUID),
        local_addr=('0.0.0.0', 1900),
        allow_broadcast=True
    )
    
    # Start Web Server
    upnp_server = UPnPServer(library, BIND_IP, PORT, UUID)
    runner = web.AppRunner(upnp_server.app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    logger.info(f"Server started on http://{BIND_IP}:{PORT}")
    
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        transport.close()
        await runner.cleanup()

if __name__ == '__main__':
    asyncio.run(main())