// Render the local editable presentation into GitHub-safe SVG images.
// Run from the repository root: node docs/diagrams/render-presentation.mjs
import { readFileSync, writeFileSync } from "node:fs";
const xml = function xml(s){return String(s).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;").replaceAll("'","&apos;");};
const presentationSvg = function presentationSvg(board,overview=false){
 const frames=board.elements.filter(e=>e.type==="frame"&&!e.isDeleted);
 const included=overview?frames.slice(0,1):frames;
 const minX=Math.min(...included.map(e=>e.x)),minY=Math.min(...included.map(e=>e.y));
 const w=Math.max(...included.map(e=>e.x+e.width))-minX,h=Math.max(...included.map(e=>e.y+e.height))-minY;
 const colors=[...new Set(board.elements.map(e=>e.strokeColor))];
 let out=`<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="${minX} ${minY} ${w} ${h}" role="img" aria-labelledby="title desc">\n<title id="title">${overview?"OpenPoke architecture presentation diagram":"OpenPoke agent overload presentation — 11 slides"}</title>\n<desc id="desc">100 active executions globally, 100 waiting, one active per Execution Agent. Original sources are retained. Historical validation: latest 9/10, earlier 10/10; 100-worker runtime checks pending.</desc>\n<defs>`;
 for(let i=0;i<colors.length;i++)out+=`<marker id="arrow${i}" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M 1 1 L 8 5 L 1 9" fill="none" stroke="${xml(colors[i])}" stroke-width="2"/></marker>`;
 for(const f of included)out+=`<clipPath id="clip-${xml(f.id)}"><rect x="${f.x}" y="${f.y}" width="${f.width}" height="${f.height}"/></clipPath>`;
 out+="</defs>\n";
 for(const f of included){
 out+=`<g clip-path="url(#clip-${xml(f.id)})" aria-label="${xml(f.name)}">\n`;
 for(const e of board.elements.filter(e=>e.frameId===f.id&&!e.isDeleted)){
 const stroke=xml(e.strokeColor),fill=xml(e.backgroundColor==="transparent"?"none":e.backgroundColor);
 if(e.type==="rectangle")out+=`<rect x="${e.x}" y="${e.y}" width="${e.width}" height="${e.height}" rx="${e.roundness?12:0}" fill="${fill}" stroke="${stroke}" stroke-width="${e.strokeWidth}"/>\n`;
 else if(e.type==="text"){
 const anchor=e.textAlign==="center"?"middle":e.textAlign==="right"?"end":"start";
 const x=e.x+(anchor==="middle"?e.width/2:anchor==="end"?e.width:0);
 out+=`<text fill="${stroke}" font-family="Excalifont, 'Comic Sans MS', Arial, sans-serif" font-size="${e.fontSize}" font-weight="${e.fontSize>=27?600:400}" text-anchor="${anchor}">`;
 e.text.split("\n").forEach((t,i)=>{out+=`<tspan x="${x}" y="${e.y+e.fontSize*.92+i*e.fontSize*(e.lineHeight??1.32)}">${xml(t)}</tspan>`;});
 out+="</text>\n";
 }else if(e.type==="line"||e.type==="arrow"){
 const points=e.points.map(([x,y])=>`${e.x+x},${e.y+y}`).join(" ");
 out+=`<polyline points="${points}" fill="none" stroke="${stroke}" stroke-width="${e.strokeWidth}" stroke-linecap="round" stroke-linejoin="round"${e.strokeStyle==="dashed"?' stroke-dasharray="7 6"':""}${e.type==="arrow"?` marker-end="url(#arrow${colors.indexOf(e.strokeColor)})"`:""}/>\n`;
 }
 }
 out+="</g>\n";
 }
 return out+"</svg>\n";
};
const board = JSON.parse(readFileSync(new URL("./openpoke-overload-solutions.excalidraw", import.meta.url), "utf8"));
writeFileSync(new URL("./openpoke-overload-solutions.svg", import.meta.url), presentationSvg(board));
writeFileSync(new URL("./openpoke-overload-overview.svg", import.meta.url), presentationSvg(board, true));
console.log("Updated the overview and full-presentation SVG images.");
