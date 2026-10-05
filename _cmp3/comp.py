import fitz,sys
def comp(name, files, page, dpi=55):
    pix=[fitz.open(f)[page].get_pixmap(dpi=dpi) for f in files]
    w=sum(p.width for p in pix)+12*(len(pix)-1); h=max(p.height for p in pix)
    out=fitz.Pixmap(fitz.csRGB, fitz.IRect(0,0,w,h), False); out.clear_with(255); x=0
    for p in pix:
        p2=p if p.n==3 and not p.alpha else fitz.Pixmap(fitz.csRGB,p)
        p2.set_origin(x,0); out.copy(p2,p2.irect); x+=p.width+12
    out.save(f'_cmp3/compare_{name}.png')
def single(name, f, page, dpi=90):
    fitz.open(f)[page].get_pixmap(dpi=dpi).save(f'_cmp3/{name}.png')
src, prefix, page, tag = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
files=[src]+[f'_cmp3/{prefix}_{e}_qwen.pdf' for e in ('babeldoc','mineru','inplace')]
comp(tag, files, page)
for label,f in zip(('src','babeldoc','mineru','inplace'),files): single(f'{tag}_{label}', f, page)
