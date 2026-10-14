import json, math, numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, matplotlib.patheffects as pe
from matplotlib.patches import Rectangle
import social_alerts as s
stats={tuple(map(int,k.split(','))):v for k,v in json.load(open('/tmp/claude-0/out/cells.json')).items()}
dists,towns=s._map_features()
south,west=s.GRID_LAT0,s.GRID_LON0
NR,NC=7,5
fig,ax=plt.subplots(figsize=(7.2,9.6),dpi=150)
ax.set_facecolor("#dfe9ee")
X=s._mx; Y=s._my
def cellrect(i,j):
    s0=s.GRID_LAT0+i*s._GRID_DLAT; w0=s.GRID_LON0+j*s._GRID_DLON
    return s0,s0+s._GRID_DLAT,w0,w0+s._GRID_DLON
cls={}
for i in range(NR):
    for j in range(NC):
        a,b,c,d=cellrect(i,j)
        tn,cv=stats.get((i,j),(0,0))
        share=tn/400.0
        if share<0.1: kind="edge"
        elif cv>=0.85: kind="ok"
        elif cv>=0.15: kind="part"
        else: kind="none"
        cls[(i,j)]=kind
# land
for n,rings in dists:
    for r in rings: ax.fill(X(r[:,0]),Y(r[:,1]),color="#f6f3ea",zorder=1,lw=0)
# cell shading
col={"part":("#f7a936",0.22,None),"none":("#8b8b8b",0.30,"////")}
for (i,j),k in cls.items():
    if k in col:
        a,b,c,d=cellrect(i,j); f,al,h=col[k]
        ax.add_patch(Rectangle((X(c),Y(a)),X(d)-X(c),Y(b)-Y(a),fc=f,alpha=al,hatch=h,ec="none",zorder=2))
for n,rings in dists:
    for r in rings: ax.plot(X(r[:,0]),Y(r[:,1]),color="#6a6a6a",lw=0.7,zorder=3)
# grid lines
for i in range(NR+1):
    ax.plot([X(west),X(west+NC*s._GRID_DLON)],[Y(south+i*s._GRID_DLAT)]*2,color="#1a1a1a",lw=1.1,zorder=4)
for j in range(NC+1):
    ax.plot([X(west+j*s._GRID_DLON)]*2,[Y(south),Y(south+NR*s._GRID_DLAT)],color="#1a1a1a",lw=1.1,zorder=4)
# radar circles
t=np.linspace(0,2*np.pi,200)
for nm,(la,lo),rad,ls in [("Chennai",(13.083747,80.290057),250,"-"),("Karaikal",(10.9254,79.838),250,"-"),("Trivandrum",(8.5374,76.8657),125,"-")]:
    ax.plot(X(lo+rad/(111.195*math.cos(math.radians(la)))*np.cos(t)),Y(la+rad/111.195*np.sin(t)),color="#1b6ec2",lw=1.6,ls=ls,alpha=0.85,zorder=5)
    ax.plot([X(lo)],[Y(la)],marker="^",ms=9,mfc="#1b6ec2",mec="white",mew=1,zorder=6)
# cell ids: column letters A.. west->east, rows 1.. north->south
halo=[pe.withStroke(linewidth=3,foreground="white")]
for (i,j),k in cls.items():
    a,b,c,d=cellrect(i,j)
    ident=f"{'ABCDE'[j]}{NR-i}"
    ax.annotate(ident,(X(c),Y(b)),xytext=(4,-4),textcoords="offset points",ha="left",va="top",fontsize=10,fontweight="bold",color="#1a1a1a",zorder=7,path_effects=halo)
# district names
for dname,rings in dists:
    r=max(rings,key=len); x,y=float(r[:,0].mean()),float(r[:,1].mean())
    ax.annotate(dname.upper(),(X(x),Y(y)),ha="center",va="center",fontsize=5.4,color="#777",zorder=4,path_effects=[pe.withStroke(linewidth=2,foreground="#f6f3ea")])
# cities
for p in towns:
    if p["kind"]!="city" or p["name"] in ("Avadi","Tambaram","Kumbakonam","Karaikudi","Sivakasi"): continue
    nm=s.PLACE_RENAME.get(p["name"],p["name"])
    off={"Chennai":(-5,6,"right"),"Tiruchirappalli":(-5,5,"right"),"Thanjavur":(5,-11,"left"),"Thoothukudi":(5,-4,"left"),
         "Tirunelveli":(-5,-4,"right"),"Coimbatore":(-5,-11,"right"),"Tirupur":(-5,-3,"right"),"Madurai":(5,-11,"left"),"Salem":(5,-11,"left"),"Kanchipuram":(5,-11,"left")}.get(nm,(4,3,"left"))
    ax.plot([X(p["lon"])],[Y(p["lat"])],"o",ms=4.5,mfc="#222",mec="white",mew=0.8,zorder=8)
    ax.annotate(nm,(X(p["lon"]),Y(p["lat"])),xytext=off[:2],textcoords="offset points",ha=off[2],fontsize=8.5,zorder=8,path_effects=halo)
ax.set_xlim(X(west),X(west+NC*s._GRID_DLON)); ax.set_ylim(Y(south),Y(south+NR*s._GRID_DLAT))
ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
fig.suptitle("Experimental Rainfall Nowcast based on IMD Radar",fontsize=15.5,fontweight="bold",y=0.985)
ax.set_title("Alert map grid: each alert map shows one 100 x 100 km square",fontsize=12,pad=8)
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
leg=[Line2D([0],[0],color="#1b6ec2",lw=2,marker="^",mfc="#1b6ec2",mec="white",ms=8,label="Radar and its range (250 km; Trivandrum 125 km)"),
     Patch(fc="#f7a936",alpha=0.35,label="Partly outside radar range"),
     Patch(fc="#8b8b8b",alpha=0.4,hatch="////",label="Outside radar range: alerts less reliable")]
fig.legend(handles=leg,loc="lower center",ncol=1,fontsize=10,frameon=False,bbox_to_anchor=(0.5,0.035))
fig.text(0.5,0.008,"Experimental, automated nowcast from IMD radar. Not an official forecast.  chennairains.com",ha="center",fontsize=9.5,color="#333")
fig.subplots_adjust(left=0.03,right=0.97,top=0.92,bottom=0.14)
fig.savefig("/tmp/claude-0/out/tn_grid_map.png",dpi=150)
import collections; print(collections.Counter(cls.values()))
print({f"{'ABCDE'[j]}{NR-i}":k for (i,j),k in cls.items() if k in("part","none")})
