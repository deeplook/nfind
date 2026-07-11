# Semantic evaluation assets

The MP3 fixtures are 0.1 seconds of generated silence with controlled ID3 metadata.
They contain no recorded or copyrighted performance.

They were created with:

```sh
ffmpeg -f lavfi -i anullsrc=r=8000:cl=mono -t 0.1 -q:a 9 \
  -metadata title="Fixture Track" -metadata artist="nfind tests" \
  -metadata album="Night Signals" -id3v2_version 3 night-signals.mp3

ffmpeg -f lavfi -i anullsrc=r=8000:cl=mono -t 0.1 -q:a 9 \
  -metadata title="Fixture Track" -metadata artist="nfind tests" \
  -metadata album="Day Signals" -id3v2_version 3 day-signals.mp3
```

The binaries are checked in so evaluation inputs do not depend on FFmpeg versions or
availability at test time. See the parent directory's `README.md` for evaluation usage.
