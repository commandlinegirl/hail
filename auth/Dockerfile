FROM {{ service_base_image.image }}

RUN hail-pip-install \
      google-auth-oauthlib==0.4.1 \
      google-auth==1.21.1

COPY auth/setup.py auth/MANIFEST.in /auth/
COPY auth/auth /auth/auth/
RUN hail-pip-install /auth && rm -rf /auth

EXPOSE 5000
