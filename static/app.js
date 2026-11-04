const $=id=>document.getElementById(id);
const keys={pexels:"",pixabay:"",eleven:"",fish:""};
const fishDefaultVoice="bf322df2096a46f18c579d0baa36f41d";
let aspect="9:16",quality="1080p",musicFile=null,activeJob=null;
const sample="Chocolate begins with cacao pods growing on trees in tropical regions. Farmers harvest the ripe pods and carefully open them to collect the cocoa beans. The beans are fermented for several days, then dried under the sun to develop their flavor. Next, they are roasted and cracked to separate the cocoa nibs from their shells. The nibs are ground into a thick paste called chocolate liquor. Sugar and other ingredients are blended in, and the mixture is refined until it becomes smooth. Finally, the chocolate is tempered, poured into molds, cooled, and packaged.";
function toast(msg){$("toast").textContent=msg;$("toast").classList.add("show");setTimeout(()=>$("toast").classList.remove("show"),2600)}
function updateCount(){const n=$("script").value.trim().split(/\s+/).filter(Boolean).length;$("wordCount").textContent=n+" words"}
$("script").addEventListener("input",updateCount);
$("sampleScript").onclick=()=>{$("topic").value="How is chocolate actually made?";$("script").value=sample;$("keywords").value="cacao pods, cocoa beans, cocoa farm, fermentation, drying cocoa, roasting, chocolate factory, chocolate bars";updateCount();toast("Sample chocolate script added.")};
document.querySelectorAll("#aspect .choice").forEach(b=>b.onclick=()=>{document.querySelectorAll("#aspect .choice").forEach(x=>x.classList.remove("selected"));b.classList.add("selected");aspect=b.dataset.value});
document.querySelectorAll("#quality button").forEach(b=>b.onclick=()=>{document.querySelectorAll("#quality button").forEach(x=>x.classList.remove("selected"));b.classList.add("selected");quality=b.dataset.value});
function openSettings(){$("settingsModal").classList.remove("hidden")}
$("settingsOpen").onclick=openSettings;$("settingsOpen2").onclick=openSettings;$("settingsClose").onclick=()=>$("settingsModal").classList.add("hidden");
$("voiceProvider").addEventListener("change",updateVoiceFields);
$("saveKeys").onclick=()=>{keys.pexels=$("pexelsKey").value.trim();keys.pixabay=$("pixabayKey").value.trim();keys.eleven=$("elevenKey").value.trim();keys.fish=$("fishKey").value.trim();refreshKeys();$("settingsModal").classList.add("hidden");toast("Keys saved in this tab session.")};
$("clearKeys").onclick=()=>{Object.keys(keys).forEach(k=>keys[k]="");["pexelsKey","pixabayKey","elevenKey","fishKey"].forEach(k=>$(k).value="");refreshKeys();toast("API keys cleared.")};
function refreshKeys(){[["pexels","Pexels"],["pixabay","Pixabay"],["eleven","ElevenLabs"],["fish","Fish Audio"]].forEach(([k])=>{$(k+"Status").textContent=keys[k]?"Key added":"Not connected";$(k+"Dot").classList.toggle("on",!!keys[k])})}
function updateVoiceFields(){const fish=$("voiceProvider").value==="fish";$("fishVoiceField").classList.toggle("hidden",!fish);$("elevenVoiceField").classList.toggle("hidden",fish)}
async function loadFishVoices(){if(!keys.fish){toast("Add your Fish Audio API key in Settings first.");openSettings();return}const button=$("loadFishVoices"),status=$("fishVoiceStatus"),select=$("fishVoiceSelect"),selected=select.value;button.disabled=true;button.textContent="Loading voices…";status.textContent="Connecting securely to Fish Audio…";try{const r=await fetch("/api/fish/voices",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({api_key:keys.fish})});const data=await r.json();if(!r.ok)throw new Error(data.detail||"Could not load voices.");select.innerHTML="";const def=document.createElement("option");def.value=fishDefaultVoice;def.textContent="Default voice · "+fishDefaultVoice;select.appendChild(def);(data.voices||[]).forEach(v=>{if(!v.id||v.id===fishDefaultVoice)return;const option=document.createElement("option");option.value=v.id;option.textContent=(v.owned?"My voice · ":"")+v.title+(v.author?" — "+v.author:"");select.appendChild(option)});select.value=[...select.options].some(o=>o.value===selected)?selected:fishDefaultVoice;status.textContent=(data.voices||[]).length+" Fish Audio voices loaded. Choose one from the list.";toast("Fish Audio voices loaded.")}catch(e){status.textContent=e.message;toast(e.message)}finally{button.disabled=false;button.textContent="Load Fish Audio voices ↻"}}
$("loadFishVoices").onclick=loadFishVoices;
$("music").onchange=e=>{musicFile=e.target.files[0]||null;$("musicName").textContent=musicFile?musicFile.name:"Add background music"};
$("musicVolume").oninput=e=>$("volumeValue").textContent=Math.round(+e.target.value*100)+"%";
$("renderBtn").onclick=async()=>{
 const topic=$("topic").value.trim(),script=$("script").value.trim(),vp=$("voiceProvider").value,sp=$("stockProvider").value;
 if(topic.length<2||script.length<20){toast("Add a topic and a longer script first.");return}
 if(!(keys.pexels||keys.pixabay)&&sp==="both"||sp==="pexels"&&!keys.pexels||sp==="pixabay"&&!keys.pixabay){toast("Add the selected stock footage API key.");openSettings();return}
 if(!keys[vp==="fish"?"fish":"eleven"]){toast("Add your selected voice provider key.");openSettings();return}
 const payload={topic,script,keywords:$("keywords").value,stock_provider:sp,pexels_key:keys.pexels,pixabay_key:keys.pixabay,voice_provider:vp,voice_key:keys[vp==="fish"?"fish":"eleven"],voice_id:vp==="fish"?$("fishVoiceSelect").value:$("voiceId").value.trim(),aspect,quality,subtitle_size:+$("subtitleSize").value||22,subtitle_color:$("subtitleColor").value,subtitle_background:$("subtitleBg").checked,music_volume:+$("musicVolume").value};
 const form=new FormData();form.append("payload",JSON.stringify(payload));if(musicFile)form.append("music",musicFile);
 $("renderBtn").disabled=true;$("renderBtn").textContent="Starting render…";$("progressPanel").classList.remove("hidden");$("result").classList.add("hidden");$("error").classList.add("hidden");$("progressTitle").textContent="Preparing your video";
 try{const res=await fetch("/api/jobs",{method:"POST",body:form});const data=await res.json();if(!res.ok)throw new Error(data.detail||"Could not start render.");activeJob=data.id;pollJob()}
 catch(e){showError(e.message);finishButton()}
};
async function pollJob(){if(!activeJob)return;try{const r=await fetch("/api/jobs/"+activeJob);const j=await r.json();$("progressTitle").textContent=j.status==="completed"?"Your video is ready":j.status==="failed"?"Render failed":"Preparing your video";$("progressStage").textContent=j.stage||"Working…";$("progressPct").textContent=(j.progress||0)+"%";$("progressFill").style.width=(j.progress||0)+"%";
 if(j.status==="completed"){$("result").classList.remove("hidden");$("downloadBtn").href=j.download_url;$("sceneSummary").textContent=(j.scenes||[]).length+" matched scenes assembled with subtitles.";finishButton();return}
 if(j.status==="failed"){showError(j.error||"The render failed.");finishButton();return}
 setTimeout(pollJob,2500)}catch(e){showError(e.message);finishButton()}}
function finishButton(){$("renderBtn").disabled=false;$("renderBtn").innerHTML="<span>✦</span> Generate another video <span>→</span>"}
function showError(msg){$("error").textContent=msg;$("error").classList.remove("hidden");$("progressPanel").classList.remove("hidden");$("progressTitle").textContent="Something needs attention";$("progressStage").textContent="Check your settings and try again."}
refreshKeys();updateCount();updateVoiceFields();