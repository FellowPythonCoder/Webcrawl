/* english.h — fast English-only filter, M4-optimized
 * Zero allocations, branch-predictor friendly, no libc heavy calls.
 * Keeps only English TLDs + content checks.
 */
#ifndef ENGLISH_H
#define ENGLISH_H

#include <string.h>
#include <strings.h>
#include <stdlib.h>

/* English-friendly TLDs — where English content actually lives.
 * Weighted: .com/.net/.org/.io/.co appear most in random generation.
 */
static const char *EN_TLDS[] = {
    "com","net","org","edu","gov","io","co","info","biz","us","uk","ca","au","nz",
    "ie","in","sg","za","ph","me","tv","app","dev","ai","tech","online","site",
    "store","blog","news","live","world","today","digital","cloud","agency",
    "studio","media","design","page","one","pro","life","works","company",
    "services","guru","email","website","art","fun","xyz","run","data","systems",
    "network","group","press","team","academy","school","science","health",
    "review","games","game","shop","club","zone","space"
};
#define EN_TLDS_N (sizeof(EN_TLDS)/sizeof(EN_TLDS[0]))

static inline int en_lower(int c){ return (c>='A'&&c<='Z')?c+32:c; }

static int en_domain_ok(const char *d){
    const char *dot = strrchr(d,'.');
    if (!dot || !dot[1]) return 0;
    if (strstr(d,"xn--")) return 0;          /* punycode */
    const char *tld = dot+1;
    for (size_t i=0;i<EN_TLDS_N;i++) if (strcmp(tld,EN_TLDS[i])==0) return 1;
    return 0;
}

static int en_find(const char *hay,int len,const char *needle){
    int nl=(int)strlen(needle);
    if (nl==0||len<nl) return -1;
    for (int i=0;i+nl<=len;i++) if (strncasecmp(hay+i,needle,(size_t)nl)==0) return i;
    return -1;
}

static int en_header_end(const char *buf,int len){
    for (int i=0;i+3<len;i++){
        if (buf[i]=='\r'&&buf[i+1]=='\n'&&buf[i+2]=='\r'&&buf[i+3]=='\n') return i+4;
        if (i+1<len && buf[i]=='\n'&&buf[i+1]=='\n') return i+2; /* tolerant */
    }
    return -1;
}

static int en_header_value(const char *buf,int hlen,const char *name,char *out,int cap){
    int nl=(int)strlen(name);
    int i=0;
    while(i<hlen){
        int e=i;
        while(e<hlen&&buf[e]!='\n') e++;
        if (e-i>nl+1 && strncasecmp(buf+i,name,(size_t)nl)==0 && buf[i+nl]==':'){
            int p=i+nl+1;
            while(p<e&&(buf[p]==' '||buf[p]=='\t')) p++;
            int n=0;
            while(p<e&&buf[p]!='\r'&&n<cap-1) out[n++]=(char)en_lower((unsigned char)buf[p++]);
            out[n]='\0';
            return 1;
        }
        i=e+1;
    }
    return 0;
}

static int en_lang_is_english(const char *v){
    while(*v==' '||*v=='\"'||*v=='\''||*v=='\t') v++;
    if (en_lower((unsigned char)v[0])!='e'||en_lower((unsigned char)v[1])!='n') return 0;
    char c=v[2];
    return c=='\0'||c=='-'||c=='_'||c==','||c==';'||c==' '||c=='\"'||c=='\''||c=='\t';
}

static int en_html_lang(const char *body,int len,char *out,int cap){
    int scan=len<4096?len:4096;
    int h=en_find(body,scan,"<html");
    if(h<0) return 0;
    int end=h;
    while(end<scan&&body[end]!='>') end++;
    int l=en_find(body+h,end-h,"lang=");
    if(l<0) return 0;
    int p=h+l+5;
    if(p<end&&(body[p]=='\"'||body[p]=='\'')) p++;
    int n=0;
    while(p<end&&body[p]!='\"'&&body[p]!='\''&&body[p]!=' '&&body[p]!='>'&&n<cap-1)
        out[n++]=(char)en_lower((unsigned char)body[p++]);
    out[n]='\0';
    return n>0;
}

static int en_is_stopword(const char *w,int n){
    static const char *SW[]={
        "the","and","of","to","in","is","for","with","that","this","on","are","as","be","by","at","or","from",
        "you","your","we","our","it","not","have","all","can","will","about","more","new","was","an","has","but",
        "they","their","home","contact","which","if","how","what","when","who","been","were","one","also","had",
        "would","there","up","out","like","just","now","into","than","only","its","over","other","some","could"
    };
    if(n<2||n>7) return 0;
    for(size_t i=0;i<sizeof(SW)/sizeof(SW[0]);i++)
        if((int)strlen(SW[i])==n&&memcmp(SW[i],w,(size_t)n)==0) return 1;
    return 0;
}

typedef struct{int words,stop,ascii,foreign;} EnScore;

static void en_score_text(const char *s,int len,EnScore *sc){
    memset(sc,0,sizeof *sc);
    char w[16]; int wl=0;
    for(int i=0;i<len;i++){
        unsigned char c=(unsigned char)s[i];
        if(c=='<'){
            int rest=len-i;
            if(rest>=4&&memcmp(s+i,"<!--",4)==0){
                int e=en_find(s+i+4,rest-4,"-->");
                i=(e<0)?len:i+4+e+2;
            }else{
                char nm[8]; int k=0,j=i+1;
                while(j<len&&k<7&&((s[j]>='a'&&s[j]<='z')||(s[j]>='A'&&s[j]<='Z'))){
                    nm[k++]=(char)en_lower((unsigned char)s[j]); j++;
                }
                nm[k]='\0';
                int is_script=(strcmp(nm,"script")==0||strcmp(nm,"style")==0||strcmp(nm,"noscript")==0);
                if(is_script){
                    char close[16]; close[0]='<'; close[1]='/'; memcpy(close+2,nm,(size_t)k+1);
                    int e=en_find(s+i,rest,close);
                    if(e<0) i=len;
                    else{
                        int g=i+e;
                        while(g<len&&s[g]!='>') g++;
                        i=g;
                    }
                }else{
                    while(i<len&&s[i]!='>') i++;
                }
            }
            goto br;
        }
        if(c=='&'){
            int j=i+1;
            while(j<len&&j-i<12&&s[j]!=';'&&s[j]!=' '&&s[j]!='<') j++;
            if(j<len&&s[j]==';') i=j;
            goto br;
        }
        if((c>='a'&&c<='z')||(c>='A'&&c<='Z')){
            sc->ascii++;
            if(wl<15) w[wl++]=(char)en_lower(c); else wl=16;
            continue;
        }
        if(c>=0xC0){
            if(c!=0xC2&&c!=0xE2) sc->foreign++; /* C2=Latin punct, E2=general punct */
            goto br;
        }
        if(c>=0x80) goto br;
br:
        if(wl>0){
            sc->words++;
            if(wl<=7&&en_is_stopword(w,wl)) sc->stop++;
            wl=0;
        }
    }
    if(wl>0){ sc->words++; if(wl<=7&&en_is_stopword(w,wl)) sc->stop++; }
}

static int english_ok(const char *buf,int len,const char *title){
    int hend=en_header_end(buf,len);
    if(hend<0) return 0;
    char v[96];
    if(en_header_value(buf,hend,"content-type",v,sizeof v)){
        if(!strstr(v,"html")&&!strstr(v,"text/plain")&&!strstr(v,"text/html")) return 0;
    }
    if(en_header_value(buf,hend,"content-language",v,sizeof v)){
        if(!en_lang_is_english(v)) return 0;
    }
    const char *body=buf+hend;
    int blen=len-hend;
    int declared_en=0;
    if(en_html_lang(body,blen,v,sizeof v)){
        if(!en_lang_is_english(v)) return 0;
        declared_en=1;
    }
    /* title check */
    {
        EnScore ts; en_score_text(title,(int)strlen(title),&ts);
        if(ts.foreign>0&&ts.foreign*100>20*(ts.ascii+ts.foreign)) return 0;
        if(ts.ascii+ts.foreign==0) return 0;
    }
    EnScore sc; en_score_text(body,blen,&sc);
    if(sc.foreign*100>12*(sc.ascii+sc.foreign)) return 0;
    if(sc.words>=40){
        int need=declared_en?6:10;
        if(sc.stop*100<need*sc.words||sc.stop<5) return 0;
        return 1;
    }
    if(declared_en) return 1;
    return sc.stop>=2&&sc.foreign==0;
}

#endif
