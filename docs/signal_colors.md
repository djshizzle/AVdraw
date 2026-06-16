# Signal Color Reference

Used consistently across draw.io exports and EasySchematic imports.

| Signal Type   | draw.io Color | EasySchematic Type | Connector     |
|---------------|---------------|--------------------|---------------|
| HDMI          | `#d6b656`     | `hdmi`             | HDMI          |
| SDI           | `#6d8764`     | `sdi`              | BNC           |
| DisplayPort   | `#0070c0`     | `displayport`      | DisplayPort   |
| USB           | `#0070c0`     | `usb`              | USB-C         |
| Ethernet/RJ45 | `#006EAF`     | `ethernet`         | RJ45          |
| Dante         | `#7030a0`     | `dante`            | RJ45          |
| NDI           | `#e36c09`     | `ndi`              | RJ45          |
| AVB           | `#833c00`     | `avb`              | RJ45          |
| Speaker Level | `#ff0000`     | `speaker-level`    | Speakon       |
| Analog Audio  | `#ff6600`     | `analog-audio`     | XLR-3         |
| RF            | `#808080`     | `rf`               | BNC           |
| Fiber         | `#00b0f0`     | `fiber`            | LC            |
| HDBaseT       | `#70ad47`     | `hdbaset`          | RJ45          |
| RS-422        | `#ffc000`     | `rs422`            | DB9           |
| GPIO          | `#ffc000`     | `gpio`             | Phoenix       |

## draw.io edge style for straight lines

Always use this style on connections in draw.io to get clean orthogonal routing:

```
edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;
exitX=1;exitY=0.5;exitDx=0;exitDy=0;
entryX=0;entryY=0.5;entryDx=0;entryDy=0;
strokeWidth=2;
strokeColor=<color>;
```

In draw.io: right-click any edge → Edit Style → paste the above.
Or set as default: right-click canvas → Edit Default Edge Style.
