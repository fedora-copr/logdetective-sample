drain didn't find the correct line with the info about missing symbol and flagged a different one that was just a warning:

```
Line 905: tiffcrop.c: In function 'writeBufferToSeparateStrips':
tiffcrop.c:1244:28: warning: comparison of integer expressions of different signedness: 'tsize_t' {aka 'long int'} and 'long long unsigned int' [-Wsign-compare]
 1244 |           if (scanlinesize > 0x0ffffffffULL) {
      |                            ^
```

The RPM build log snippet indicates a compiler warning during the build process of the 'tiffcrop.c' file. The warning message specifically mentions a comparison of two data types with different signedness in line 1244 of the file: 'tsize_t' (a long int type) and 'long long unsigned int'. The compiler is advising that such a comparison may result in unexpected behavior due to potential sign conversions. The warning message concludes with the location of the issue in the code.
