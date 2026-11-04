import contextlib
import gzip
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1]/'capture'
loader=importlib.machinery.SourceFileLoader('capture_module',str(PATH))
spec=importlib.util.spec_from_loader(loader.name,loader);cap=importlib.util.module_from_spec(spec);loader.exec_module(cap)

class CaptureTests(unittest.TestCase):
    def frame(self, data, seq, response=False, link=1):
        src,dst=(b'\x7f\x00\x00\x01',b'\x7f\x00\x00\x02')
        sport,dport=(12345,8000)
        if response: src,dst=dst,src;sport,dport=dport,sport
        tcp=struct.pack('!HHIIBBHHH',sport,dport,seq,0,0x50,0x18,65535,0,0)+data
        ip=struct.pack('!BBHHHBBH4s4s',0x45,0,len(tcp)+20,1,0,64,6,0,src,dst)+tcp
        prefix={0:b'\x02\x00\x00\x00',1:b'\0'*12+b'\x08\x00',113:b'\0'*14+b'\x08\x00',276:b'\x08\x00'+b'\0'*18}[link]
        return prefix+ip

    def write_pcap(self,path,frames,link):
        out=struct.pack('<IHHIIII',0xa1b2c3d4,2,4,0,0,65535,link)
        for i,(payload,seq,response) in enumerate(frames):
            frame=self.frame(payload,seq,response,link)
            out+=struct.pack('<IIII',100,i*100000,len(frame),len(frame))+frame
        path.write_bytes(out)

    def test_link_types_retransmission_out_of_order_and_gzip(self):
        request=b'GET /items HTTP/1.1\r\nCookie: secret\r\nHost: app\r\n\r\n'
        body=gzip.compress(b'hello')
        response=b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body
        for link in [0,1,113,276]:
            with self.subTest(link=link),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'a.pcap';out=Path(tmp)/'a.jsonl'
                self.write_pcap(path,[(request[20:],120,False),(request[:20],100,False),(request[:20],100,False),(response,500,True)],link)
                cap.pcap(path,out);rows=[json.loads(l) for l in out.read_text().splitlines()]
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['uri'],'/items')
                self.assertEqual(rows[0]['response']['body'],'hello')

    def test_chunked_pipelined_and_body_limit(self):
        body=b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n'
        rows=list(cap.messages(body,[(0,1)]))
        self.assertEqual(len(rows),2);self.assertEqual(rows[0]['body'],'hello')
        data=b'x'*(cap.MAX_BODY+1)
        rows=list(cap.messages(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data,[(0,1)]))
        self.assertEqual(len(rows[0]['body']),cap.MAX_BODY);self.assertTrue(rows[0]['body_truncated'])

    def test_gap_is_not_joined(self):
        data,times=cap.reassemble([(1,b'GET ',1),(10,b'/ HTTP/1.1\r\n\r\n',2)])
        self.assertEqual(data,b'GET ');self.assertEqual(list(cap.messages(data,times)),[])

    def test_flows_hash_credentials_and_relative_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'access.log';groups=Path(tmp)/'groups'
            groups.write_text('^/items/[0-9]+\n')
            p.write_text('\n'.join(json.dumps({'time':t,'cookie':'VERY_SECRET','uri':uri,'method':'GET'}) for t,uri in [(100,'/'),(102,'/items/123?token=SECRET')]))
            with contextlib.redirect_stdout(io.StringIO()) as out: cap.flows(p,'cookie',groups)
            text=out.getvalue();self.assertNotIn('SECRET',text);self.assertIn('+2.000s 間隔 2.000s',text)
            self.assertIn('^/items/[0-9]+',text)

    def test_truncated_pcap_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'bad';p.write_bytes(b'bad')
            with self.assertRaises(ValueError): list(cap.packets(p))

if __name__=='__main__':unittest.main()
