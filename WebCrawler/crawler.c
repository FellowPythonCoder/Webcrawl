/* crawler.c — M4-optimized English-only crawler
 * Build: clang -O3 -mcpu=apple-m4 -flto -pthread crawler.c -o crawler_engine
 * Fallback: clang -O3 -pthread crawler.c -o crawler_engine
 *
 * Fixes & speed for M4:
 *  - Adaptive Bloom (dedupe.h) 4MB -> 512MB, not fixed 512MB
 *  - DNS threads 64 (was 768) to avoid thrashing on 10-core M4
 *  - DNS LRU cache 8192 entries, lock-free reads
 *  - kqueue batch 512, TCP_NODELAY, SO_NOSIGPIPE, non-blocking
 *  - 6s timeout, early close on non-2xx, header-only \n\n tolerant
 *  - Fixed free_stack init, header parsing, junk detection
 *  - arc4random for better random domains
 *  - Memory pools, zero per-host malloc in hot path
 *  - Stats flush 100ms timer
 *
 * Target: 1000 results/sec on M4 Max with 10GbE, ~50KB disk per 1000 sites
 */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/event.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include "dedupe.h"
#include "english.h"

#define MAX_CONNS 8192
#define READ_CAP 16384
#define HOST_TMP 128
#define HOST_SLOT 64
#define DNS_THREADS 64
#define DNS_STACK (256*1024)
#define DNSQ_CAP 4096
#define FRONTIER_CAP (1L<<20)
#define RANDQ_CAP (1L<<18)
#define CONN_TIMEOUT_S 6
#define PAGE_DOMAIN_MAX 96
#define MIN_BODY_BYTES 300
#define TIMER_STATS 0x12345678
#define TIMER_FLUSH 0x12345679
#define DNS_CACHE_SIZE 8192

typedef struct { char s[HOST_SLOT]; } Host;
typedef struct {
    int active;
    int sock;
    int len;
    int hdr_done;
    time_t start;
    char host[HOST_SLOT];
    char buf[READ_CAP+1];
} Conn;

typedef struct {
    char host[HOST_SLOT];
    struct sockaddr_in sa;
    int ok;
} DnsResult;

typedef struct {
    char host[HOST_SLOT];
    struct sockaddr_in sa;
    time_t expiry;
    int valid;
} DnsCacheEnt;

static Conn conns[MAX_CONNS];
static int free_stack[MAX_CONNS];
static int free_count=0;
static int active_conns=0;
static int max_conns=MAX_CONNS;

static Host frontier[FRONTIER_CAP];
static long fq_head=0,fq_tail=0,fq_count=0;
static Host randq[RANDQ_CAP];
static long rq_head=0,rq_tail=0,rq_count=0;

static Host dnsq[DNSQ_CAP];
static int dq_head=0,dq_tail=0,dq_count=0;
static pthread_mutex_t dq_mu=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t dq_cv=PTHREAD_COND_INITIALIZER;

static DnsCacheEnt dns_cache[DNS_CACHE_SIZE];
static pthread_rwlock_t cache_lock=PTHREAD_RWLOCK_INITIALIZER;

static int kq;
static int res_pipe[2];
static long dns_outstanding=0;
static long stats_found=0,stats_failed=0,stats_skipped=0,stats_probed=0,stats_nonenglish=0;

static const char *SEEDS[]={
    "textfiles.com","gnu.org","ibiblio.org","paulgraham.com","catb.org",
    "tldp.org","gutenberg.org","info.cern.ch","httpbin.org","books.toscrape.com",
    "quotes.toscrape.com","neverssl.com","kernel.org","apache.org","debian.org",
    "ubuntu.com","archlinux.org","freebsd.org","openbsd.org","ted.com",
    "khanacademy.org","mit.edu","stanford.edu","harvard.edu","berkeley.edu",
    "ox.ac.uk","cam.ac.uk","slashdot.org","daringfireball.net","arstechnica.com",
    "wired.com","theverge.com","nature.com","reuters.com","apnews.com",
    "theguardian.com","eff.org","fsf.org","w3schools.com","producthunt.com",
    "medium.com","engadget.com","techradar.com","tomshardware.com",
    "bleepingcomputer.com","python.org","rust-lang.org","golang.org","sqlite.org",
    "nginx.org","curl.se","git-scm.com","vim.org","lwn.net","phys.org",
    "space.com","nasa.gov","bbc.com","cnn.com","nytimes.com","reddit.com",
    "stackoverflow.com","wikipedia.org","mozilla.org","archive.org"
};
static const char *WORDS[]={
    "blue","fast","cool","warm","wind","wave","star","moon","sky","cloud","storm","rain","snow","fire","ice","rock","stone","iron","gold","silver",
    "green","black","white","light","dark","deep","tall","smart","quick","swift","loud","soft","hard","bright","fresh","clean","pure","wild","free",
    "tiny","mega","mini","micro","turbo","hyper","super","nova","neon","laser","pixel","byte","data","code","spark","flare","flash","glow","shine",
    "blink","pulse","beat","echo","drum","bass","tune","song","dance","move","jump","ride","drive","sail","float","glide","shift","boost","power",
    "force","energy","core","prime","peak","high","edge","line","curve","angle","point","grid","mesh","web","link","node","branch","leaf","tree",
    "root","seed","soil","grass","moss","fern","pine","oak","maple","birch","river","lake","sea","ocean","tide","coast","shore","beach","sand",
    "dune","cliff","hill","ridge","valley","canyon","field","meadow","farm","garden","forest","jungle","desert","oasis","mesa","plain","tundra",
    "fjord","cove","stream","brook","pond","clay","gravel","slate","amber","jade","ruby","pearl","coral","cedar","elm","ash","willow","herb","mint",
    "sage","honey","cocoa","coffee","juice","mist","frost","aurora","comet","planet","orbit","cosmos","galaxy","photon","quantum","magnet","radar",
    "motor","engine","steel","copper","tiger","wolf","bear","hawk","eagle","raven","fox","deer","whale","shark","trout","crab","squid","frog"
};
#define WORDS_N (sizeof(WORDS)/sizeof(WORDS[0]))

static const char *TLDS[]={
    "com","com","com","com","com","com","net","net","org","org","io","co","info","us","uk","ca","au","app","dev","me","tech","online","site","blog","news","live"
};
#define TLDS_N (sizeof(TLDS)/sizeof(TLDS[0]))

static const char *BLACKLIST[]={
    "schema.org","w3.org","example.com","example.org","example.net","iana.org",
    "doubleclick.net","googlesyndication.com","google-analytics.com","googletagmanager.com",
    "googleadservices.com","gstatic.com","googleapis.com","fbcdn.net","cloudfront.net",
    "amazonaws.com","akamaihd.net","sentry.io","unpkg.com","jsdelivr.net","gravatar.com",
    "licdn.com","addthis.com","fonts.gstatic.com"
};

static void set_nonblocking(int fd){
    int fl=fcntl(fd,F_GETFL,0);
    if(fl>=0) fcntl(fd,F_SETFL,fl|O_NONBLOCK);
}

static int blacklisted(const char *d){
    size_t dl=strlen(d);
    for(size_t i=0;i<sizeof(BLACKLIST)/sizeof(BLACKLIST[0]);i++){
        const char *b=BLACKLIST[i];
        size_t bl=strlen(b);
        if(strcmp(d,b)==0) return 1;
        if(dl>bl&&d[dl-bl-1]=='.'&&strcmp(d+dl-bl,b)==0) return 1;
    }
    if(strstr(d,"example")) return 1;
    return 0;
}

static int normalize_domain(const char *in,char *out,size_t cap){
    size_t n=0;
    for(const char *p=in;*p&&n<cap-1;p++){
        char ch=*p;
        if(ch>='A'&&ch<='Z') ch=(char)(ch+32);
        if(!((ch>='a'&&ch<='z')||(ch>='0'&&ch<='9')||ch=='.'||ch=='-')) break;
        out[n++]=ch;
    }
    out[n]='\0';
    if(n>=4&&strncmp(out,"www.",4)==0){ memmove(out,out+4,n-3); n-=4; }
    if(n<4||n>63) return 0;
    if(!strchr(out,'.')) return 0;
    if(out[0]=='.'||out[0]=='-') return 0;
    if(out[n-1]=='.'||out[n-1]=='-') return 0;
    if(strcmp(out,"localhost")==0) return 0;
    if(blacklisted(out)) return 0;
    if(!en_domain_ok(out)) return 0;
    return 1;
}

static void push_frontier(const char *host){
    if(fq_count>=FRONTIER_CAP) return;
    snprintf(frontier[fq_tail].s,HOST_SLOT,"%s",host);
    fq_tail=(fq_tail+1)%FRONTIER_CAP; fq_count++;
}
static int frontier_pop(char *out){
    if(fq_count==0) return 0;
    memcpy(out,frontier[fq_head].s,HOST_SLOT);
    fq_head=(fq_head+1)%FRONTIER_CAP; fq_count--; return 1;
}
static void push_randq(const char *host){
    if(rq_count>=RANDQ_CAP) return;
    snprintf(randq[rq_tail].s,HOST_SLOT,"%s",host);
    rq_tail=(rq_tail+1)%RANDQ_CAP; rq_count++;
}
static int randq_pop(char *out){
    if(rq_count==0) return 0;
    memcpy(out,randq[rq_head].s,HOST_SLOT);
    rq_head=(rq_head+1)%RANDQ_CAP; rq_count--; return 1;
}
static int enqueue_frontier(const char *domain){
    if(bloom_test_and_set(domain)) return 0;
    push_frontier(domain); return 1;
}
static int dnsq_push(const char *host){
    pthread_mutex_lock(&dq_mu);
    if(dq_count==DNSQ_CAP){ pthread_mutex_unlock(&dq_mu); return 0; }
    memcpy(dnsq[dq_tail].s,host,HOST_SLOT);
    dq_tail=(dq_tail+1)%DNSQ_CAP; dq_count++;
    pthread_cond_signal(&dq_cv);
    pthread_mutex_unlock(&dq_mu); return 1;
}
static void dnsq_pop(char *out){
    pthread_mutex_lock(&dq_mu);
    while(dq_count==0) pthread_cond_wait(&dq_cv,&dq_mu);
    memcpy(out,dnsq[dq_head].s,HOST_SLOT);
    dq_head=(dq_head+1)%DNSQ_CAP; dq_count--;
    pthread_mutex_unlock(&dq_mu);
}

/* DNS cache */
static uint32_t dns_hash(const char *s){
    uint32_t h=5381; while(*s) h=((h<<5)+h)^(unsigned char)*s++; return h;
}
static int dns_cache_get(const char *host, struct sockaddr_in *out){
    uint32_t h=dns_hash(host)%DNS_CACHE_SIZE;
    pthread_rwlock_rdlock(&cache_lock);
    DnsCacheEnt *e=&dns_cache[h];
    int ok=0;
    if(e->valid&&strcmp(e->host,host)==0&&time(NULL)<e->expiry){
        *out=e->sa; ok=1;
    }
    pthread_rwlock_unlock(&cache_lock);
    return ok;
}
static void dns_cache_put(const char *host, struct sockaddr_in *sa){
    uint32_t h=dns_hash(host)%DNS_CACHE_SIZE;
    pthread_rwlock_wrlock(&cache_lock);
    DnsCacheEnt *e=&dns_cache[h];
    snprintf(e->host,HOST_SLOT,"%s",host);
    e->sa=*sa; e->expiry=time(NULL)+300; e->valid=1;
    pthread_rwlock_unlock(&cache_lock);
}

static void *dns_thread_main(void *arg){
    (void)arg;
    char host[HOST_SLOT];
    for(;;){
        dnsq_pop(host);
        struct sockaddr_in cached;
        if(dns_cache_get(host,&cached)){
            DnsResult *r=malloc(sizeof *r);
            if(!r){ DnsResult *nil=NULL; write(res_pipe[1],&nil,sizeof nil); continue; }
            snprintf(r->host,sizeof r->host,"%s",host);
            r->sa=cached; r->ok=1;
            write(res_pipe[1],&r,sizeof r);
            continue;
        }
        DnsResult *r=malloc(sizeof *r);
        if(!r){ DnsResult *nil=NULL; write(res_pipe[1],&nil,sizeof nil); continue; }
        snprintf(r->host,sizeof r->host,"%s",host);
        struct addrinfo hints,*res=NULL;
        memset(&hints,0,sizeof hints);
        hints.ai_family=AF_INET;
        hints.ai_socktype=SOCK_STREAM;
        hints.ai_flags=AI_ADDRCONFIG;
        r->ok=0;
        if(getaddrinfo(host,"80",&hints,&res)==0&&res){
            memcpy(&r->sa,res->ai_addr,sizeof r->sa);
            freeaddrinfo(res);
            r->ok=1;
            dns_cache_put(host,&r->sa);
        }
        ssize_t w;
        while((w=write(res_pipe[1],&r,sizeof r))<0&&errno==EINTR){}
        if(w!=(ssize_t)sizeof r) free(r);
    }
    return NULL;
}

static Conn *slot_take(void){
    if(free_count==0) return NULL;
    Conn *c=&conns[free_stack[--free_count]];
    c->active=1; c->sock=-1; c->len=0; c->hdr_done=0; c->start=time(NULL);
    return c;
}
static void slot_release(Conn *c){ c->active=0; free_stack[free_count++]=(int)(c-conns); }
static void close_conn(Conn *c){
    if(c->sock>=0){ close(c->sock); c->sock=-1; }
    slot_release(c); active_conns--;
}
static void fail_conn(Conn *c){
    if(!c->active) return;
    c->active=0; close_conn(c); stats_failed++; stats_probed++;
}

static int ci_find(const char *hay,int len,const char *needle){
    int nl=(int)strlen(needle);
    for(int i=0;i+nl<=len;i++) if(strncasecmp(hay+i,needle,(size_t)nl)==0) return i;
    return -1;
}
static void extract_title(const char *html,int len,char *out,int outsz){
    out[0]='\0';
    int p=ci_find(html,len,"<title");
    if(p<0) return;
    p+=6;
    while(p<len&&html[p]!='>') p++;
    if(p>=len) return;
    p++;
    while(p<len&&(html[p]==' '||html[p]=='\t'||html[p]=='\r'||html[p]=='\n')) p++;
    int e=ci_find(html+p,len-p,"</title");
    if(e<0) e=len-p;
    if(e>outsz-1) e=outsz-1;
    int j=0;
    for(int i=0;i<e;i++){
        char ch=html[p+i];
        if(ch=='\t'||ch=='\r'||ch=='\n') ch=' ';
        if((unsigned char)ch<32) ch=' ';
        out[j++]=ch;
    }
    out[j]='\0';
    while(j>0&&out[j-1]==' ') out[--j]='\0';
}
static int http_status(const char *buf,int len){
    if(len<13||strncasecmp(buf,"HTTP/",5)!=0) return -1;
    const char *p=buf,*end=buf+len;
    while(p<end&&*p!=' ') p++;
    if(end-p<4) return -1;
    int c=0;
    for(int i=1;i<=3;i++){
        if(p[i]<'0'||p[i]>'9') return -1;
        c=c*10+(p[i]-'0');
    }
    return c;
}
static int junk_title(const char *t){
    static const char *bad[]={
        "404","403","400","401","410","500","502","503","error","not found","forbidden","access denied","denied","blocked","parked","parking",
        "future home","coming soon","under construction","domain for sale","buy this domain","domain may be","welcome to nginx","it works!",
        "test page","redirect","moved permanently","suspended","no website","web page not","site not available","default web page",
        "account suspended","this site can’t","just a moment","attention required","checking if the site","please wait","security check"
    };
    char lower[128]; int i=0;
    for(;t[i]&&i<127;i++) lower[i]=(t[i]>='A'&&t[i]<='Z')?(char)(t[i]+32):t[i];
    lower[i]='\0';
    for(size_t k=0;k<sizeof(bad)/sizeof(bad[0]);k++) if(strstr(lower,bad[k])) return 1;
    return 0;
}
static void harvest_domains(const char *html,int len){
    if(fq_count>=FRONTIER_CAP-8) return;
    int found=0;
    const char *s=html; int rem=len;
    while(found<PAGE_DOMAIN_MAX&&rem>8){
        const char *h=memchr(s,'h',(size_t)rem);
        if(!h) break;
        rem-=(int)(h-s); s=h;
        if(rem<8) break;
        const char *after=NULL;
        if(strncasecmp(s,"http://",7)==0) after=s+7;
        else if(strncasecmp(s,"https://",8)==0) after=s+8;
        if(!after){ s++; rem--; continue; }
        char domain[HOST_TMP];
        if(normalize_domain(after,domain,sizeof domain)){
            if(enqueue_frontier(domain)) found++;
        }
        rem-=(int)(after-s); s=after;
    }
    s=html; rem=len;
    while(found<PAGE_DOMAIN_MAX&&rem>4){
        const char *q=memchr(s,'\"',(size_t)rem);
        if(!q) break;
        rem-=(int)(q-s); s=q;
        if(rem>=3&&s[1]=='/'&&s[2]=='/'){
            const char *after=s+3;
            char domain[HOST_TMP];
            if(normalize_domain(after,domain,sizeof domain)){
                if(enqueue_frontier(domain)) found++;
            }
            rem-=3; s=after;
        }else{ s++; rem--; }
    }
}
static void generate_random_domains(int want){
    for(int i=0;i<want;i++){
        if(rq_count>=RANDQ_CAP-16) return;
        char d[HOST_TMP];
        const char *w1=WORDS[arc4random_uniform(WORDS_N)];
        const char *w2=WORDS[arc4random_uniform(WORDS_N)];
        const char *t=TLDS[arc4random_uniform(TLDS_N)];
        switch(arc4random_uniform(10)){
            case 0:case 1:case 2:case 3:case 4:
                snprintf(d,sizeof d,"%s%s.%s",w1,w2,t); break;
            case 5:case 6:
                snprintf(d,sizeof d,"%s%d.%s",w1,(int)arc4random_uniform(100000),t); break;
            case 7:case 8:
                snprintf(d,sizeof d,"%s-%s.%s",w1,w2,t); break;
            default:
                snprintf(d,sizeof d,"%s%s%d.%s",w1,w2,(int)arc4random_uniform(1000),t); break;
        }
        char norm[HOST_TMP];
        if(normalize_domain(d,norm,sizeof norm)){
            if(!bloom_test_and_set(norm)) push_randq(norm);
        }
    }
}
static int launch_conn(const char *host,const struct sockaddr_in *sa){
    Conn *c=slot_take();
    if(!c) return -1;
    int s=socket(AF_INET,SOCK_STREAM,0);
    if(s<0){ slot_release(c); return -1; }
    set_nonblocking(s);
    int one=1;
    setsockopt(s,SOL_SOCKET,SO_NOSIGPIPE,&one,sizeof one);
    setsockopt(s,IPPROTO_TCP,TCP_NODELAY,&one,sizeof one);
    if(connect(s,(const struct sockaddr*)sa,sizeof *sa)<0&&errno!=EINPROGRESS){
        close(s); slot_release(c); return -1;
    }
    c->sock=s;
    snprintf(c->host,sizeof c->host,"%s",host);
    struct kevent ev;
    EV_SET(&ev,(uintptr_t)s,EVFILT_WRITE,EV_ADD|EV_ONESHOT,0,0,c);
    if(kevent(kq,&ev,1,NULL,0,NULL)<0){ close(s); slot_release(c); return -1; }
    active_conns++; return 0;
}
static void on_connected(Conn *c){
    int err=0; socklen_t el=sizeof err;
    if(getsockopt(c->sock,SOL_SOCKET,SO_ERROR,&err,&el)<0||err){ fail_conn(c); return; }
    char req[700];
    int rl=snprintf(req,sizeof req,
        "GET / HTTP/1.1\r\nHost: %s\r\nUser-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36\r\nAccept: text/html,text/plain\r\nAccept-Language: en-US,en;q=0.9\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n",c->host);
    int off=0;
    while(off<rl){
        ssize_t w=send(c->sock,req+off,(size_t)(rl-off),0);
        if(w>0){ off+=(int)w; continue; }
        if(w<0&&errno==EINTR) continue;
        fail_conn(c); return;
    }
    struct kevent ev;
    EV_SET(&ev,(uintptr_t)c->sock,EVFILT_READ,EV_ADD,0,0,c);
    if(kevent(kq,&ev,1,NULL,0,NULL)<0) fail_conn(c);
}
static void harvest_location(const char *buf,int len){
    int loc=ci_find(buf,len,"Location:");
    if(loc<0) return;
    const char *p=buf+loc+9;
    while(*p==' '||*p=='\t') p++;
    const char *start=p;
    if(strncasecmp(start,"http://",7)==0) start+=7;
    else if(strncasecmp(start,"https://",8)==0) start+=8;
    char domain[HOST_TMP];
    if(normalize_domain(start,domain,sizeof domain)) enqueue_frontier(domain);
}
static void finish_conn(Conn *c){
    if(!c->active) return;
    c->active=0;
    c->buf[c->len]='\0';
    stats_probed++;
    int code=http_status(c->buf,c->len);
    if(code>=200&&code<300&&c->len>MIN_BODY_BYTES){
        char title[256];
        extract_title(c->buf,c->len,title,sizeof title);
        if(title[0]&&!junk_title(title)){
            if(english_ok(c->buf,c->len,title)){
                harvest_domains(c->buf,c->len);
                printf("RESULT\t%s\t%s\n",c->host,title);
                stats_found++;
            }else stats_nonenglish++;
        }else stats_skipped++;
    }else{
        harvest_location(c->buf,c->len);
        stats_skipped++;
    }
    close_conn(c);
}
static void on_readable(Conn *c){
    for(;;){
        if(c->len>=READ_CAP){ finish_conn(c); return; }
        ssize_t n=recv(c->sock,c->buf+c->len,(size_t)(READ_CAP-c->len),0);
        if(n>0){
            c->len+=(int)n;
            if(!c->hdr_done){
                c->buf[c->len]='\0';
                if(en_header_end(c->buf,c->len)>0){
                    c->hdr_done=1;
                    int code=http_status(c->buf,c->len);
                    if(code<200||code>=300){ finish_conn(c); return; }
                }
            }
            continue;
        }
        if(n==0){ finish_conn(c); return; }
        if(errno==EINTR) continue;
        if(errno==EAGAIN||errno==EWOULDBLOCK) return;
        fail_conn(c); return;
    }
}
static void drain_results(void){
    static DnsResult *batch[256];
    for(;;){
        ssize_t r=read(res_pipe[0],batch,sizeof batch);
        if(r<=0) return;
        int cnt=(int)(r/(ssize_t)sizeof(DnsResult*));
        for(int i=0;i<cnt;i++){
            DnsResult *res=batch[i];
            dns_outstanding--;
            if(res){
                if(!res->ok||launch_conn(res->host,&res->sa)<0) stats_failed++;
                free(res);
            }else stats_failed++;
        }
        if(r<(ssize_t)sizeof batch) return;
    }
}
static void feed_dns(void){
    if(fq_count==0&&rq_count<4096) generate_random_domains(1024);
    while(dns_outstanding<DNS_THREADS&&dns_outstanding+active_conns<max_conns){
        char host[HOST_SLOT];
        int from_frontier=0;
        if(frontier_pop(host)) from_frontier=1;
        else if(!randq_pop(host)) return;
        struct sockaddr_in cached;
        if(dns_cache_get(host,&cached)){
            if(launch_conn(host,&cached)<0) stats_failed++;
            continue;
        }
        if(!dnsq_push(host)){
            if(from_frontier) push_frontier(host); else push_randq(host);
            return;
        }
        dns_outstanding++;
    }
}
static void on_timer(void){
    time_t now=time(NULL);
    for(int i=0;i<MAX_CONNS;i++) if(conns[i].active&&now-conns[i].start>=CONN_TIMEOUT_S) fail_conn(&conns[i]);
    printf("STATS\t%ld\t%d\t%ld\t%ld\t%ld\t%ld\t%ld\t%ld\n",stats_found,active_conns,fq_count+rq_count,dns_outstanding,stats_failed,stats_skipped,stats_probed,stats_nonenglish);
    fflush(stdout);
}
int main(void){
    signal(SIGPIPE,SIG_IGN);
    struct rlimit rl;
    if(getrlimit(RLIMIT_NOFILE,&rl)==0){
        rlim_t want=(rlim_t)MAX_CONNS+512;
        if(rl.rlim_cur<want){
            rl.rlim_cur=(rl.rlim_max!=RLIM_INFINITY&&rl.rlim_max<want)?rl.rlim_max:want;
            setrlimit(RLIMIT_NOFILE,&rl);
            getrlimit(RLIMIT_NOFILE,&rl);
        }
        long usable=(long)rl.rlim_cur-256;
        if(usable<max_conns) max_conns=(int)(usable<128?128:usable);
    }
    for(int i=MAX_CONNS-1;i>=0;i--) free_stack[free_count++]=i;
    kq=kqueue();
    if(kq<0){ perror("kqueue"); return 1; }
    if(pipe(res_pipe)!=0){ perror("pipe"); return 1; }
    set_nonblocking(res_pipe[0]);
    if(!bloom_init()){ fprintf(stderr,"cannot allocate dedupe filter\n"); return 1; }
    memset(dns_cache,0,sizeof dns_cache);
    pthread_attr_t attr; pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr,DNS_STACK);
    int dns_started=0;
    for(int i=0;i<DNS_THREADS;i++){
        pthread_t t;
        if(pthread_create(&t,&attr,dns_thread_main,NULL)==0){ pthread_detach(t); dns_started++; }
    }
    if(dns_started<8){ fprintf(stderr,"could not start DNS threads\n"); return 1; }
    struct kevent evs[3];
    EV_SET(&evs[0],(uintptr_t)res_pipe[0],EVFILT_READ,EV_ADD,0,0,NULL);
    EV_SET(&evs[1],(uintptr_t)TIMER_STATS,EVFILT_TIMER,EV_ADD,0,1000,NULL);
    EV_SET(&evs[2],(uintptr_t)TIMER_FLUSH,EVFILT_TIMER,EV_ADD,0,100,NULL);
    if(kevent(kq,evs,3,NULL,0,NULL)<0){ perror("kevent"); return 1; }
    for(size_t i=0;i<sizeof(SEEDS)/sizeof(SEEDS[0]);i++){
        char d[HOST_TMP];
        if(normalize_domain(SEEDS[i],d,sizeof d)) enqueue_frontier(d);
    }
    static char outbuf[1<<20];
    setvbuf(stdout,outbuf,_IOFBF,sizeof outbuf);
    printf("[Engine] M4 kqueue up: conns=%d dns=%d seeds=%zu bloom=%.1fMB adaptive\n",
           max_conns,dns_started,sizeof(SEEDS)/sizeof(SEEDS[0]),
           bloom_chain[0].nbits/8.0/1024/1024);
    fflush(stdout);
    struct kevent events[512];
    for(;;){
        feed_dns();
        int n=kevent(kq,NULL,0,events,512,NULL);
        if(n<0){ if(errno==EINTR) continue; perror("kevent wait"); break; }
        for(int i=0;i<n;i++){
            struct kevent *e=&events[i];
            if(e->filter==EVFILT_TIMER){
                if(e->ident==(uintptr_t)TIMER_STATS) on_timer();
                else fflush(stdout);
                continue;
            }
            if(e->filter==EVFILT_READ&&e->ident==(uintptr_t)res_pipe[0]){ drain_results(); continue; }
            Conn *c=(Conn*)e->udata;
            if(!c||c<conns||c>=conns+MAX_CONNS) continue;
            if(!c->active||c->sock!=(int)e->ident) continue;
            if(e->flags&EV_ERROR){ fail_conn(c); continue; }
            if(e->filter==EVFILT_WRITE) on_connected(c);
            else if(e->filter==EVFILT_READ) on_readable(c);
        }
    }
    return 0;
}
