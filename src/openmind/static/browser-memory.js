(function (root) {
  "use strict";

  function formatBytes(bytes) {
    if (bytes === null || !Number.isFinite(bytes)) return "Unavailable";
    if (bytes < 1024) return `${Math.round(bytes)} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / 1024 ** 2).toFixed(2)} MiB`;
  }

  // Counters and a bounded history only. No frame, Blob or pixel arrays retained.
  function createFrameLedger() {
    return {
      source: "", width: 0, height: 0, encoded: null, updates: 0,
      heap: null, history: [], generation: 0,
      frame(source, width, height) {
        if (!source || !width || !height) return;
        if (source !== this.source) {
          this.updates += 1;
          this.encoded = null;
          this.generation += 1;
        }
        this.source = source;
        this.width = width;
        this.height = height;
      },
      encodedSize(source, generation, size) {
        if (source === this.source && generation === this.generation) this.encoded = size;
      },
      clear() {
        this.source = "";
        this.width = this.height = 0;
        this.encoded = null;
        this.generation += 1;
      },
      sample(heap) {
        this.heap = Number.isFinite(heap) && heap >= 0 ? heap : null;
        this.history.push(this.heap === null ? this.width * this.height * 4 : this.heap);
        if (this.history.length > 60) this.history.shift();
      },
    };
  }

  const component = {
    emits: ["sampleMemory"],
    data: () => ({ ledger: createFrameLedger() }),
    computed: {
      pixelBytes() { return formatBytes(this.ledger.width * this.ledger.height * 4); },
      encodedBytes() { return formatBytes(this.ledger.encoded); },
      heapBytes() { return formatBytes(this.ledger.heap); },
      points() {
        const samples = this.ledger.history;
        const max = Math.max(1, ...samples);
        return samples.map((value, i) => `${i * 4},${30 - value / max * 27}`).join(" ");
      },
    },
    mounted() {
      this._active = true;
      this._image = null;
      this._fetching = false;
      this._onLoad = (event) => {
        if (event.target instanceof HTMLImageElement && event.target.closest(".om-canvas")) {
          this.readImage(event.target);
        }
      };
      document.addEventListener("load", this._onLoad, true);
      this._timer = setInterval(() => this.sample(), 1000);
      this.sample();
    },
    beforeUnmount() {
      this._active = false;
      clearInterval(this._timer);
      document.removeEventListener("load", this._onLoad, true);
      if (this._abort) this._abort.abort();
      this._image = null;
    },
    methods: {
      readImage(image) {
        if (!image.isConnected || !image.complete || !image.naturalWidth) return;
        this._image = image;
        this.ledger.frame(image.currentSrc || image.src, image.naturalWidth, image.naturalHeight);
      },
      async sample() {
        const image = document.querySelector(".om-canvas img");
        if (image) this.readImage(image);
        else if (this.ledger.source) {
          if (this._abort) this._abort.abort();
          this._image = null;
          this.ledger.clear();
        }
        this.ledger.sample(root.performance && root.performance.memory
          ? root.performance.memory.usedJSHeapSize : null);
        this.$emit("sampleMemory");
        if (!image || this._fetching || this.ledger.encoded !== null || !this.ledger.source.startsWith("blob:")) return;
        const source = this.ledger.source;
        const generation = this.ledger.generation;
        this._fetching = true;
        this._abort = new AbortController();
        try {
          // Inspect only the current local Blob, at most once per second.
          // No server request, ArrayBuffer, pixel copy or saved frame history.
          const response = await fetch(source, { signal: this._abort.signal });
          const blob = await response.blob();
          if (this._active && image.isConnected && (image.currentSrc || image.src) === source) {
            this.ledger.encodedSize(source, generation, blob.size);
          }
        } catch (_) {
          // Fast interaction can revoke a previous frame before it is sampled.
        } finally {
          this._fetching = false;
        }
      },
    },
    template: `
      <section class="om-browser-memory" aria-label="Browser memory monitor">
        <div class="om-memory-title">BROWSER · THIS TAB</div>
        <div>Volume voxels <b>0 B</b></div>
        <div>Visible image pixels, estimate <b>{{ pixelBytes }}</b></div>
        <div>Current encoded image <b>{{ encodedBytes }}</b></div>
        <div>Displayed image updates <b>{{ ledger.updates }}</b></div>
        <div>JS heap, approximate <b>{{ heapBytes }}</b></div>
        <div>Total tab / GPU memory <b>Not exposed</b></div>
        <svg viewBox="0 0 236 32" role="img" :aria-label="ledger.heap === null ? 'Last 60 seconds of visible image pixel estimates' : 'Last 60 seconds of approximate JavaScript heap readings'" class="om-memory-chart">
          <polyline :points="points" fill="none" stroke="#9bbdc8" stroke-width="1.5" />
        </svg>
        <div class="om-memory-note">{{ ledger.heap === null ? '60 s: visible image pixels' : '60 s: approximate JS heap' }}. Images replace each other. Other browser/VTK buffers are not counted.</div>
      </section>`,
  };

  const plugin = {
    install(app) { app.component("openmind-memory", component); },
    createFrameLedger, formatBytes,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = plugin;
  else root.openmindMemory = plugin;
})(typeof window === "undefined" ? globalThis : window);
