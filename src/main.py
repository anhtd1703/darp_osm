import argparse, collections, json, math, random, statistics, time
from pathlib import Path
from core import *

def extract(cfg,pbf,seed):
    import osmium
    from shapely.geometry import shape, Point
    areas=DATA/'areas.geojson'; zones=[]
    if areas.exists():
        for f in json.loads(areas.read_text())['features']:
            zones.append((f['properties']['city'],f['properties']['name'],shape(f['geometry'])))
    def locate(x,y):
        if zones:
            for city,name,g in zones:
                if g.covers(Point(x,y)): return city,name
            return None
        for city,c in cfg['cities'].items():
            a,b,d,e=c['bbox']
            if a<=x<=d and b<=y<=e:
                if c.get('approx_zones'):
                    distances=[(math.hypot((x-lon)*111*math.cos(math.radians(lat)),(y-lat)*111),name,radius) for name,(lon,lat,radius) in c['approx_zones'].items()]
                    eligible=[(dist,name) for dist,name,radius in distances if dist<=radius]
                    if eligible:return city,'approx_'+min(eligible)[1]
                    continue
                return city,'bbox_'+city
        return None
    class Handler(osmium.SimpleHandler):
        def __init__(self): super().__init__(); self.rows=[]
        def node(self,n):
            tags=n.tags; name=tags.get('name'); category=next((tags.get(k) for k in ('amenity','shop','tourism','leisure','office') if tags.get(k)),None)
            if not name or not category or not n.location.valid(): return
            x,y=n.location.lon,n.location.lat; z=locate(x,y)
            if not z:return
            city,zone=z; address=' '.join(filter(None,[tags.get('addr:housenumber'),tags.get('addr:street'),tags.get('addr:place'),tags.get('addr:city')]))
            self.rows.append(dict(osm_id=f'node/{n.id}',city=city,zone=zone,name=name,address=address or name,address_source='OSM tags' if address else 'POI name only',category=category,lon=x,lat=y,kind='poi'))
    h=Handler(); h.apply_file(str(pbf)); rng=random.Random(seed); pool=[]
    for city,c in cfg['cities'].items():
        candidates=sorted([p for p in h.rows if p['city']==city],key=lambda p:p['osm_id']); rng.shuffle(candidates)
        # Round robin across spatial zones, avoiding dominance by one district.
        buckets=collections.defaultdict(collections.deque)
        for p in candidates:buckets[p['zone']].append(p)
        candidates=[]
        while any(buckets.values()):
            for zone in sorted(buckets):
                if buckets[zone]:candidates.append(buckets[zone].popleft())
        count=0; used=set()
        for p in candidates:
            s=nearest(p['lon'],p['lat'],cfg['max_snap_m'])
            if not s:continue
            xy=(round(s[0],6),round(s[1],6))
            if xy in used:continue
            used.add(xy); p.update(route_lon=s[0],route_lat=s[1],snap_m=round(s[2],2)); pool.append(p); count+=1
            if count>=cfg['pool_size_per_city']:break
        if count<max(max(a) for b in cfg['profiles'].values() for a in b.values())+5: raise RuntimeError(f'{city}: only {count} usable POIs; expand area or inspect road access')
        x,y=c['depot']; s=nearest(x,y,cfg['max_snap_m'])
        if not s:raise RuntimeError(f'{city}: depot is too far from road; edit config')
        pool.append(dict(osm_id='depot/'+city,city=city,zone='depot',name='Depot '+city,address='Assumed depot',address_source='config',category='depot',lon=x,lat=y,kind='depot',route_lon=s[0],route_lat=s[1],snap_m=round(s[2],2)))
        print(city,count,'POIs')
    write_csv(DATA/'poi_pool.csv',pool)
    digest=hashlib.sha256()
    with open(pbf,'rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    (DATA/'osm_manifest.json').write_text(json.dumps({'pbf':str(pbf),'sha256':digest.hexdigest(),'zones':'geojson' if zones else 'approximate','seed':seed},indent=2))

def synthetic_pool(cfg):
    rng=random.Random(2026); rows=[]
    for city,c in cfg['cities'].items():
        x,y=c['depot']
        for i in range(120):
            # Fake coordinates solely for smoke test; names explicitly fictional.
            a=x+rng.uniform(-.025,.025); b=y+rng.uniform(-.025,.025)
            rows.append(dict(osm_id=f'fake/{city}/{i}',city=city,zone='synthetic',name=f'Fake {city} {i}',address='Synthetic test only',lon=a,lat=b,route_lon=a,route_lat=b,kind='poi'))
        rows.append(dict(osm_id='depot/'+city,city=city,zone='synthetic',name='Depot '+city,address='Synthetic',lon=x,lat=y,route_lon=x,route_lat=y,kind='depot'))
    return rows

def run_day(cfg,rows,pool,method,mode,traffic,turnaround):
    depots={p['city']:(float(p['route_lon']),float(p['route_lat'])) for p in pool if p['kind']=='depot'}
    fleet=[dict(id=f'{c}{i+1:02d}',city=c,ready=0) for c in ('TB','HN') for i in range(cfg['fleet_per_city'])]
    by=collections.defaultdict(list)
    for r in rows:by[(int(r['slot_sec']),r['direction'])].append(r)
    trips=[]; passengers=[]; routes=[]; scenario=rows[0]['scenario']; seed=rows[0]['seed']
    for h in range(cfg['first_hour'],cfg['last_hour']+1):
        slot=h*3600
        for a,b in [('TB','HN'),('HN','TB')]:
            direction=a+'-'+b; rs=by[(slot,direction)]; available=sorted([v for v in fleet if v['city']==a and v['ready']<=slot],key=lambda v:(v['ready'],v['id']))
            record=dict(scenario=scenario,seed=seed,method=method,mode=mode,traffic=traffic,turnaround_min=turnaround,slot_sec=slot,direction=direction,demand=len(rs),vehicle_id='',served=0,distance_km=0,duration_min=0,solver_ms=0,matrix_ms=0,cost_vnd=0,departed=0,status='no_vehicle',arrive_sec='',ready_sec='')
            served={}; reason='no_vehicle'
            if available:
                v=available[0]; tick=time.perf_counter(); P=build_problem(rs,a,b,depots,cfg,mode,traffic,slot); record['matrix_ms']=round((time.perf_counter()-tick)*1000,3)
                tick=time.perf_counter()
                if method=='greedy' or not rs: result=greedy(P,rs); status='greedy' if rs else 'empty_reposition'
                else: result,status=optimize(P,rs,cfg['solver_seconds'])
                record['solver_ms']=round((time.perf_counter()-tick)*1000,3)
                if result is None:raise RuntimeError('No valid route including repositioning')
                served={m['request_id']:m for m in result['served']}; reason='not_selected_or_infeasible'
                arrival=slot+result['duration_sec']; v.update(city=b,ready=arrival+turnaround*60)
                km=result['distance_m']/1000
                record.update(vehicle_id=v['id'],served=len(served),distance_km=km,duration_min=result['duration_sec']/60,cost_vnd=round(km*cfg['cost_per_km_vnd']+cfg['cost_per_trip_vnd']),departed=1,status=status,arrive_sec=arrival,ready_sec=v['ready'])
                routes.append(dict(**record,points=[dict(lon=P['points'][j][0],lat=P['points'][j][1],kind=P['nodes'][j]['kind'],request_id=P['nodes'][j].get('request_id',''),time_sec=slot+t) for j,t in zip(result['route'],result['times'])]))
            trips.append(record)
            for r in rs:
                m=served.get(r['request_id']); row=dict(scenario=scenario,seed=seed,method=method,mode=mode,traffic=traffic,turnaround_min=turnaround,request_id=r['request_id'],direction=direction,slot_sec=slot,vehicle_id=record['vehicle_id'] if m else '',served=int(bool(m)),on_time=int(bool(m)),reason='' if m else reason,pickup_sec='',dropoff_sec='',ride_min='',direct_min='',detour_min='',pickup_delay_min='')
                if m:row.update(pickup_sec=slot+m['pickup_sec'],dropoff_sec=slot+m['dropoff_sec'],ride_min=m['ride_sec']/60,direct_min=m['direct_sec']/60,detour_min=m['detour_sec']/60,pickup_delay_min=m['pickup_delay_sec']/60)
                passengers.append(row)
    return trips,passengers,routes

def p90(xs):
    return sorted(xs)[math.ceil(.9*len(xs))-1] if xs else None

def summarize(trips,passengers,cfg):
    def avg(xs):return statistics.mean(xs) if xs else None
    served=[r for r in passengers if r['served']]; n=len(passengers); dep=[r for r in trips if r['departed']]
    return {**{k:trips[0][k] for k in ('scenario','seed','method','mode','traffic','turnaround_min')},'demand':n,'served':len(served),'rejected':n-len(served),'served_pct':100*len(served)/n if n else None,'ontime_all_pct':100*sum(r['on_time'] for r in passengers)/n if n else None,'ontime_served_pct':100*sum(r['on_time'] for r in served)/len(served) if served else None,'scheduled_trips':len(trips),'departed_trips':len(dep),'missed_trips':len(trips)-len(dep),'empty_trips':sum(r['served']==0 for r in dep),'vehicles_used':len({r['vehicle_id'] for r in dep}),'seat_fill_pct':100*len(served)/(len(dep)*cfg['capacity']) if dep else None,'distance_km':sum(r['distance_km'] for r in dep),'cost_vnd':sum(r['cost_vnd'] for r in dep),'cost_per_passenger_vnd':sum(r['cost_vnd'] for r in dep)/len(served) if served else None,'avg_ride_min':avg([r['ride_min'] for r in served]),'avg_detour_min':avg([r['detour_min'] for r in served]),'p90_detour_min':p90([r['detour_min'] for r in served]),'avg_pickup_delay_min':avg([r['pickup_delay_min'] for r in served]),'p90_pickup_delay_min':p90([r['pickup_delay_min'] for r in served]),'avg_trip_duration_min':avg([r['duration_min'] for r in dep]),'solver_total_ms':sum(r['solver_ms'] for r in dep),'matrix_total_ms':sum(r['matrix_ms'] for r in dep),'fallback_trips':sum(r['status'].startswith('fallback') for r in dep)}

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('extract'); p.add_argument('--pbf',default='data/vietnam-latest.osm.pbf'); p.add_argument('--seed',type=int,default=2026)
    for name in ('generate','benchmark'):
        p=sub.add_parser(name); p.add_argument('--scenarios',nargs='+',default=['normal']); p.add_argument('--seeds',nargs='+',type=int,default=[11]); p.add_argument('--jitter',type=int,default=0); p.add_argument('--synthetic',action='store_true')
        if name=='benchmark':
            p.add_argument('--traffic',nargs='+',type=float,default=[1.0]); p.add_argument('--turnaround',nargs='+',type=int); p.add_argument('--methods',nargs='+',choices=['greedy','ortools'],default=['greedy','ortools']); p.add_argument('--fleet-per-city',type=int); p.add_argument('--trip-limit',type=int); p.add_argument('--output',default='results/run')
    p=sub.add_parser('map'); p.add_argument('--routes',default='results/run/routes.json'); p.add_argument('--output',default='results/map.html'); p.add_argument('--method',default='ortools'); p.add_argument('--hour',type=int,default=7); p.add_argument('--direction',default='TB-HN'); p.add_argument('--scenario'); p.add_argument('--seed',type=int); p.add_argument('--traffic',type=float); p.add_argument('--turnaround',type=int)
    args=ap.parse_args(); cfg=json.loads(Path('config.json').read_text()); DATA.mkdir(exist_ok=True); OUT.mkdir(exist_ok=True)
    if args.cmd=='extract':extract(cfg,args.pbf,args.seed); return
    if args.cmd=='map':
        import folium
        allr=json.loads(Path(args.routes).read_text()); rr=[r for r in allr if r['method']==args.method and r['slot_sec']==args.hour*3600 and r['direction']==args.direction and (args.scenario is None or r['scenario']==args.scenario) and (args.seed is None or int(r['seed'])==args.seed) and (args.traffic is None or r['traffic']==args.traffic) and (args.turnaround is None or r['turnaround_min']==args.turnaround)]
        if not rr:raise SystemExit('No matching departed route')
        r=rr[0]; pts=r['points']; xy=[(p['lon'],p['lat']) for p in pts]; mp=folium.Map(location=[pts[0]['lat'],pts[0]['lon']],zoom_start=9,tiles='OpenStreetMap')
        geom=api('/route/v1/driving/'+coords_string(xy),overview='full',geometries='geojson')['routes'][0]['geometry']['coordinates'] if r['mode']=='osrm' else xy
        folium.PolyLine([(y,x) for x,y in geom],color='blue',weight=5).add_to(mp)
        for i,p in enumerate(pts):folium.Marker([p['lat'],p['lon']],tooltip=f"{i}: {p['kind']} {p['request_id']} {p['time_sec']//3600:02d}:{p['time_sec']//60%60:02d}",icon=folium.Icon(color={'pickup':'blue','dropoff':'green','start':'black','end':'red'}[p['kind']])).add_to(mp)
        mp.fit_bounds([(p['lat'],p['lon']) for p in pts]); Path(args.output).parent.mkdir(parents=True,exist_ok=True); mp.save(args.output); print(args.output); return
    pool=synthetic_pool(cfg) if args.synthetic else read_csv(DATA/'poi_pool.csv'); mode='synthetic' if args.synthetic else 'osrm'
    if args.cmd=='benchmark':
        if args.fleet_per_city is not None:cfg['fleet_per_city']=args.fleet_per_city
        if args.trip_limit is not None:cfg['trip_limit_min']=args.trip_limit
    if args.jitter<0:raise ValueError('jitter must be >=0')
    if cfg['headway_min']!=60:raise ValueError('This version supports hourly slots only')
    if cfg['fleet_per_city']<1 or cfg['trip_limit_min']<1:raise ValueError('Invalid fleet/trip duration')
    if args.cmd=='benchmark' and (any(t<=0 for t in args.traffic) or any(x<0 for x in (args.turnaround or []))):raise ValueError('Traffic must be positive and turnaround nonnegative')
    days=[make_requests(pool,cfg,s,seed,args.jitter) for s in args.scenarios for seed in args.seeds]; allreq=[r for day in days for r in day]
    if args.cmd=='generate':
        write_csv(DATA/f'requests_{mode}.csv',allreq);write_csv(DATA/f'stops_{mode}.csv',stops_csv(allreq));print(len(allreq),'requests');return
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True); write_csv(out/'requests.csv',allreq);write_csv(out/'stops.csv',stops_csv(allreq)); write_csv(out/'pool.csv',pool)
    (out/'config_used.json').write_text(json.dumps(cfg,indent=2)); summaries=[]; trips=[]; passengers=[]; routes=[]
    for day in days:
        for traffic in args.traffic:
            for turn in args.turnaround if args.turnaround is not None else [cfg['turnaround_min']]:
                for method in args.methods:
                    t,p,r=run_day(cfg,day,pool,method,mode,traffic,turn); sm=summarize(t,p,cfg);summaries.append(sm);trips+=t;passengers+=p;routes+=r; print({k:sm[k] for k in ('scenario','seed','method','traffic','turnaround_min','served','missed_trips','distance_km')},flush=True)
    write_csv(out/'summary.csv',summaries)
    grouped=collections.defaultdict(dict)
    for sm in summaries:grouped[tuple(sm[k] for k in ('scenario','seed','traffic','turnaround_min'))][sm['method']]=sm
    paired=[]
    for key,methods in grouped.items():
        if 'greedy' not in methods or 'ortools' not in methods:continue
        a,b=methods['greedy'],methods['ortools']; pair=dict(zip(('scenario','seed','traffic','turnaround_min'),key))
        for k in ('served','served_pct','missed_trips','distance_km','cost_vnd','cost_per_passenger_vnd','avg_detour_min','p90_detour_min','avg_pickup_delay_min','solver_total_ms'):
            pair[k+'_as_is']=a[k];pair[k+'_to_be']=b[k];pair[k+'_delta']=b[k]-a[k] if a[k] is not None and b[k] is not None else None
        paired.append(pair)
    if paired:write_csv(out/'paired_comparison.csv',paired)
    write_csv(out/'trips.csv',trips);write_csv(out/'passengers.csv',passengers);(out/'routes.json').write_text(json.dumps(routes,ensure_ascii=False));print(out)
if __name__=='__main__':main()
