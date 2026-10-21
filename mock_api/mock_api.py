#!/usr/bin/env python3
"""Local mock REST API for testing the API-to-CSV utility. Uses only Python stdlib."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import json, argparse

def build_orders(count=20):
    products=[("Laptop",2),("Mouse",3),("Keyboard",1),("Monitor",2),("Headset",1)]
    cities=[("Chennai","India"),("Bangalore","India"),("Hyderabad","India"),("Mumbai","India")]
    orders=[]
    for i in range(1,count+1):
        city,country=cities[(i-1)%len(cities)]
        items=[]
        for j in range((i%3)+1):
            product,qty=products[(i+j)%len(products)]
            suppliers=[{
                "supplier_id":f"S{((i+j)%4)+1:03d}",
                "name":["ABC Supplies","XYZ Global","PQR Traders","LMN Corp"][(i+j)%4],
                "country":["India","Singapore","India","UAE"][(i+j)%4]
            }]
            if j==0 and i%2==0:
                suppliers.append({"supplier_id":"S999","name":"Backup Supplier","country":"India"})
            items.append({
                "line_number":j+1,"product":product,"qty":qty,
                "price":round(100+i*17.25+j*50,2),"suppliers":suppliers
            })
        orders.append({
            "order_id":10000+i,
            "order_date":f"2026-10-{(i%28)+1:02d}",
            "status":"ACTIVE" if i%4 else "CLOSED",
            "customer":{
                "customer_id":5000+i,"name":["John","Raj","Priya","Anita"][i%4],
                "email":f"customer{i}@example.com",
                "address":{"city":city,"country":country,"postal_code":f"6000{i:02d}"}
            },
            "items":items,
            "shipping":{"method":"EXPRESS" if i%2==0 else "STANDARD",
                        "address":{"city":city,"country":country}}
        })
    return orders

ORDERS=build_orders()

class Handler(BaseHTTPRequestHandler):
    def send_json(self,status,payload):
        body=json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n=int(self.headers.get("Content-Length","0"))
        return json.loads(self.rfile.read(n).decode()) if n else {}

    def do_GET(self):
        p=urlparse(self.path); q=parse_qs(p.query)
        if p.path=="/health":
            return self.send_json(200,{"status":"UP"})
        if p.path!="/orders":
            return self.send_json(404,{"error":"Not found"})
        page=int(q.get("page",["1"])[0]); size=int(q.get("page_size",["5"])[0])
        status=q.get("status",[None])[0]
        orders=[o for o in ORDERS if not status or o["status"]==status]
        start=(page-1)*size; page_items=orders[start:start+size]
        nxt=None
        if start+size<len(orders):
            nxt=f"http://localhost:{self.server.server_port}/orders?page={page+1}&page_size={size}"
        return self.send_json(200,{
            "request":{"page":page,"page_size":size,"status":status},
            "meta":{"page":page,"page_size":size,"total_records":len(orders),"next_url":nxt},
            "orders":page_items
        })

    def do_POST(self):
        p=urlparse(self.path)
        if p.path!="/orders/search":
            return self.send_json(404,{"error":"Not found"})
        try: body=self.read_json()
        except Exception: return self.send_json(400,{"error":"Invalid JSON"})
        status=body.get("status"); min_qty=body.get("min_qty")
        orders=[o for o in ORDERS if not status or o["status"]==status]
        if min_qty is not None:
            orders=[dict(o,items=[i for i in o["items"] if i["qty"]>=int(min_qty)])
                    for o in orders]
            orders=[o for o in orders if o["items"]]
        return self.send_json(200,{"request":body,"result":{"count":len(orders),"orders":orders},
                                  "server":{"name":"PowerCenter API Test Server","version":"1.0"}})

    def log_message(self,fmt,*args): print("[MOCK-API]",fmt%args)

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--host",default="127.0.0.1"); ap.add_argument("--port",type=int,default=8000)
    a=ap.parse_args()
    s=ThreadingHTTPServer((a.host,a.port),Handler)
    print(f"Mock API running at http://{a.host}:{a.port}")
    print("GET /health | GET /orders | POST /orders/search")
    try: s.serve_forever()
    except KeyboardInterrupt: pass
    finally: s.server_close()
