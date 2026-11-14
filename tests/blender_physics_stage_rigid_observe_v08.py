"""Independent collision/rest checks on the full saved production trajectory."""
from pathlib import Path
import bpy,json,sys,math
from mathutils import Vector
from mathutils.bvhtree import BVHTree
out=Path(sys.argv[sys.argv.index('--')+1]).resolve()
data=json.loads((out/'build.json').read_text());states=json.loads((out/'states.json').read_text())
bpy.ops.wm.open_mainfile(filepath=str(out/'output.blend'),use_scripts=False)
small='200 container' in data['scope'];checks={};report={}
late=[states[str(f)] for f in range(230,251)]
speed=max(Vector(v).length for row in late for v in row['velocities'])
angle=max((Vector(a)-Vector(b)).length for f in range(230,251) for a,b in zip(states[str(f)]['rotations'],states[str(f-1)]['rotations']))
report.update(late_max_speed=speed,late_max_rotation_delta=angle)
checks['late_speed_below_sleep']=speed<=data['request']['parameters']['sleep_velocity']
checks['late_rotation_delta']=angle<=1e-4
if small:
 collider=bpy.data.objects['ContainerAndRamp'];m=collider.data
 tree=BVHTree.FromPolygons([collider.matrix_world@v.co for v in m.vertices],[list(p.vertices) for p in m.polygons],all_triangles=False)
 minimum=math.inf;worst=None;bounces=[]
 # The ramp is z=.1+(x-.3)*1.1/2.35, with normal facing -X/+Z.
 normal=Vector((-1.1,0,2.35)).normalized()
 outside=0
 for frame in range(12,251):
  row=states[str(frame)];before=states[str(frame-1)]
  for index,(position,velocity) in enumerate(zip(row['positions'],row['velocities'])):
   p=Vector(position);v=Vector(velocity);distance=tree.find_nearest(p)[3]
   if distance<minimum:minimum=distance;worst={'frame':frame,'index':index,'position':position}
   outside+=int(abs(p.x)>2.5+1e-4 or abs(p.y)>2.5+1e-4)
   prev=Vector(before['velocities'][index]);ramp_distance=(p-Vector((.3,0,.1))).dot(normal)
   if .3<=p.x<=2.65 and abs(p.y)<=2.65 and abs(ramp_distance-.2)<.15 and prev.dot(normal)<-.1 and v.dot(normal)>.01 and v.x<-.01:
    bounces.append({'frame':frame,'index':index,'before_normal_speed':prev.dot(normal),'after_normal_speed':v.dot(normal),'ramp_distance':ramp_distance})
 report.update(min_mesh_distance=minimum,worst_distance=worst,outside_container_samples=outside,ramp_bounces=bounces)
 checks.update(mesh_clearance=minimum>=.2-1e-4,container_containment=outside==0,observed_ramp_bounce=bool(bounces))
report.update(checks=checks,ok=all(checks.values()))
(out/'contact-rest-observations.json').write_text(json.dumps(report,indent=2))
print(json.dumps({k:v for k,v in report.items() if k!='ramp_bounces'}));assert report['ok'],checks
