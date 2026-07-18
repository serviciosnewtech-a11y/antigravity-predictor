FROM nginx:alpine

# Copy static dashboard files
COPY dashboard/index.html  /usr/share/nginx/html/
COPY dashboard/app.js      /usr/share/nginx/html/
COPY dashboard/style.css   /usr/share/nginx/html/

# Copy nginx config
COPY dashboard/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
