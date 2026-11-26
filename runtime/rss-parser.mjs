// Parse only; all network requests belong to the Python polling service.
import Parser from 'rss-parser';
const chunks=[];let bytes=0;
process.stdin.on('data',chunk=>{bytes+=chunk.length;if(bytes>10485760)process.exit(2);chunks.push(chunk);});
process.stdin.on('end',async()=>{
 try{
  const feed=await new Parser().parseString(Buffer.concat(chunks).toString('utf8'));
  if(feed.items.length>10000)throw new Error('Feed exceeds 10,000 items.');
  const pick=(value,keys)=>Object.fromEntries(keys.filter(k=>value[k]!==undefined).map(k=>[k,value[k]]));
  const fields=['title','link','pubDate','guid','description','content','contentSnippet','author','categories','enclosure','isoDate'];
  const result={feed:pick(feed,['title','link','description']),items:feed.items.map(item=>pick({...item,author:item.author||item.creator},fields))};
  const encoded=JSON.stringify(result);
  if(Buffer.byteLength(encoded)>20971520)throw new Error('Parsed feed exceeds 20 MiB.');
  process.stdout.write(JSON.stringify({ok:true,...result}));
 }catch(e){process.stdout.write(JSON.stringify({ok:false,error:String(e.message??e).slice(0,2000)}));}
});
