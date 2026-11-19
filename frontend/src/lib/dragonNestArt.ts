import genericGif from "../assets/dragon-nest.gif";
import genericPng from "../assets/dragon-nest.png";
import coderGif from "../assets/dragon-nests/coder.gif";
import coderPng from "../assets/dragon-nests/coder.png";
import qualitySamplerGif from "../assets/dragon-nests/quality-sampler.gif";
import qualitySamplerPng from "../assets/dragon-nests/quality-sampler.png";
import runnerGif from "../assets/dragon-nests/runner.gif";
import runnerPng from "../assets/dragon-nests/runner.png";
import observerGif from "../assets/dragon-nests/observer.gif";
import observerPng from "../assets/dragon-nests/observer.png";

type NestArt = { gif: string; png: string };
const generic: NestArt = { gif: genericGif, png: genericPng };
const qualitySampler: NestArt = { gif: qualitySamplerGif, png: qualitySamplerPng };
const artByIdentity = new Map<string, NestArt>([
  ["Coder", { gif: coderGif, png: coderPng }],
  ["QualitySampler", qualitySampler],
  ["QS", qualitySampler],
  ["Runner", { gif: runnerGif, png: runnerPng }],
  ["Observer", { gif: observerGif, png: observerPng }],
]);

/** Use the exact displayed group identity, not a model, workstream, or name substring. */
export function dragonNestArt(identity: string): NestArt {
  return artByIdentity.get(identity) ?? generic;
}
